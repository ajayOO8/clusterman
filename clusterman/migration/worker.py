# Copyright 2019 Yelp Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import time
from functools import partial
from multiprocessing import Process
from multiprocessing.synchronize import Lock as LockBase
from statistics import mean
from typing import Callable
from typing import cast
from typing import Collection

import colorlog

from clusterman.autoscaler.pool_manager import AWS_RUNNING_STATES
from clusterman.autoscaler.pool_manager import PoolManager
from clusterman.autoscaler.toggle import disable_autoscaling
from clusterman.autoscaler.toggle import enable_autoscaling
from clusterman.draining.queue import TerminationReason
from clusterman.interfaces.types import ClusterNodeMetadata
from clusterman.kubernetes.kubernetes_cluster_connector import KubernetesClusterConnector
from clusterman.migration.event import MigrationEvent
from clusterman.migration.settings import WorkerSetup
from clusterman.monitoring_lib import get_monitoring_client
from clusterman.util import limit_function_runtime


logger = colorlog.getLogger(__name__)
UPTIME_CHECK_INTERVAL_SECONDS = 60 * 60  # 1 hour
INITIAL_POOL_HEALTH_TIMEOUT_SECONDS = 15 * 60
SUPPORTED_POOL_SCHEDULER = "kubernetes"

SFX_NODE_DRAIN_COUNT = "clusterman.node_migration.drain_count"
SFX_MIGRATION_JOB_DURATION = "clusterman.node_migration.duration"
SFX_DRAINED_NODE_UPTIME = "clusterman.node_migration.drained_node_uptime"


class RestartableDaemonProcess:
    def __init__(self, target, args, kwargs) -> None:
        self.__target = target
        self.__args = args
        self.__kwargs = kwargs
        self._init_proc_handle()

    def _init_proc_handle(self):
        self.process_handle = Process(target=self.__target, args=self.__args, kwargs=self.__kwargs)
        self.process_handle.daemon = True

    def restart(self):
        if self.process_handle.is_alive():
            self.process_handle.kill()
        self._init_proc_handle()
        self.process_handle.start()

    def __getattr__(self, attr):
        return getattr(self.process_handle, attr)


class NodeMigrationError(Exception):
    pass


def _monitor_pool_health(
    manager: PoolManager,
    timeout: float,
    drained: Collection[ClusterNodeMetadata],
    health_check_interval_seconds: int,
    ignore_pod_health: bool = False,
) -> bool:
    """Monitor pool health after nodes were submitted for draining

    :param PoolManager manager: pool manager instance
    :param float timeout: timestamp after which giving up
    :param Collection[ClusterNodeMetadata] drained: nodes which were submitted for draining
    :param bool ignore_pod_health: If set, do not check that pods can successfully be scheduled
    :return: true if capacity is fulfilled
    """
    draining_happened, capacity_satisfied, pods_healthy = False, False, False
    connector = cast(KubernetesClusterConnector, manager.cluster_connector)
    logger.info(f"Monitoring health for {manager.cluster}:{manager.pool}")
    while time.time() < timeout:
        manager.reload_state(load_pods_info=not ignore_pod_health)
        draining_happened = draining_happened or not any(
            node.agent.agent_id == connector.get_agent_metadata(node.instance.ip_address).agent_id for node in drained
        )
        # TODO: replace these with use of walrus operator in if-statement once on py38
        capacity_satisfied = capacity_satisfied or (draining_happened and manager.is_capacity_satisfied())
        pods_healthy = pods_healthy or (
            draining_happened and (ignore_pod_health or connector.has_enough_capacity_for_pods())
        )
        if draining_happened and capacity_satisfied and pods_healthy:
            return True
        else:
            logger.info(
                f"Pool {manager.cluster}:{manager.pool} not healthy yet"
                f" (drain_ok={draining_happened}, capacity_ok={capacity_satisfied}, pods_ok={pods_healthy})"
            )
        time.sleep(health_check_interval_seconds)
    return False


def _drain_node_selection(
    manager: PoolManager, selector: Callable[[ClusterNodeMetadata], bool], worker_setup: WorkerSetup
) -> bool:
    """Drain nodes in pool according to selection criteria

    :param PoolManager manager: pool manager instance
    :param Callable[[ClusterNodeMetadata], bool] selector: selection filter
    :param WorkerSetup worker_setup: node migration setup
    :return: true if completed
    """
    nodes = manager.get_node_metadatas(AWS_RUNNING_STATES)
    selected = sorted(filter(selector, nodes), key=worker_setup.precedence.sort_key)
    if not selected:
        return True
    monitoring_info = {"cluster": manager.cluster, "pool": manager.pool}
    node_drain_counter = get_monitoring_client().create_counter(SFX_NODE_DRAIN_COUNT, monitoring_info)
    job_timer = get_monitoring_client().create_timer(SFX_MIGRATION_JOB_DURATION, monitoring_info)
    node_uptime_gauge = get_monitoring_client().create_gauge(SFX_DRAINED_NODE_UPTIME, monitoring_info)
    chunk = worker_setup.rate.of(len(nodes))
    logger.info(f"{len(selected)} nodes of {manager.cluster}:{manager.pool} will be recycled")
    job_timer.start()
    for i in range(0, len(selected), chunk):
        start_time = time.time()
        selection_chunk = selected[i : i + chunk]
        for node in selection_chunk:
            logger.info(f"Recycling node {node.instance.instance_id}")
            manager.submit_for_draining(node, TerminationReason.NODE_MIGRATION)
            node_uptime_gauge.set(node.instance.uptime.total_seconds())
            node_drain_counter.count()
        time.sleep(worker_setup.bootstrap_wait)
        if not _monitor_pool_health(
            manager=manager,
            timeout=start_time + worker_setup.bootstrap_timeout,
            drained=selection_chunk,
            health_check_interval_seconds=worker_setup.health_check_interval,
            ignore_pod_health=worker_setup.ignore_pod_health,
        ):
            logger.warning(
                f"Pool {manager.cluster}:{manager.pool} did not come back"
                " to desired capacity, stopping selection draining"
            )
            job_timer.stop()
            return False
        logger.info(f"Recycled {min(i + chunk, len(selected))} nodes out of {len(selected)} selected")
    logger.info(f"Completed recycling node selection from {manager.cluster}:{manager.pool}")
    job_timer.stop()
    return True


def uptime_migration_worker(
    cluster: str, pool: str, uptime_seconds: int, worker_setup: WorkerSetup, pool_lock: LockBase
) -> None:
    """Worker monitoring and migrating nodes according to uptime

    :parma str cluster: cluster name
    :param str pool: pool name
    :param int uptime_seconds: uptime threshold
    :param WorkerSetup worker_setup: migration setup
    """
    manager = PoolManager(cluster, pool, SUPPORTED_POOL_SCHEDULER)
    node_selector = lambda node: node.instance.uptime.total_seconds() > uptime_seconds  # noqa
    if not manager.draining_client:
        logger.warning(f"Draining client not set up for {cluster}:{pool}, giving up")
        return
    while True:
        if manager.is_capacity_satisfied():
            with pool_lock:
                _drain_node_selection(manager, node_selector, worker_setup)
        else:
            logger.warning(f"Pool {cluster}:{pool} is currently underprovisioned, skipping uptime migration iteration")
        time.sleep(UPTIME_CHECK_INTERVAL_SECONDS)
        manager.reload_state(load_pods_info=not worker_setup.ignore_pod_health)


def event_migration_worker(migration_event: MigrationEvent, worker_setup: WorkerSetup, pool_lock: LockBase) -> None:
    """Worker migrating nodes according to event configuration

    :param MigrationEvent migration_event: event instance
    :param WorkerSetup worker_setup: migration setup
    """
    pool_lock_acquired = False
    manager = PoolManager(migration_event.cluster, migration_event.pool, SUPPORTED_POOL_SCHEDULER, fetch_state=False)
    connector = cast(KubernetesClusterConnector, manager.cluster_connector)
    connector.set_label_selectors(migration_event.label_selectors, add_to_existing=True)
    manager.reload_state(load_pods_info=not worker_setup.ignore_pod_health)
    try:
        pool_lock.acquire(timeout=worker_setup.expected_duration)
        pool_lock_acquired = True
        if worker_setup.disable_autoscaling:
            logger.info(f"Disabling autoscaling for {migration_event.cluster}:{migration_event.pool}")
            disable_autoscaling(
                migration_event.cluster,
                migration_event.pool,
                SUPPORTED_POOL_SCHEDULER,
                time.time() + worker_setup.expected_duration,
            )
        if worker_setup.prescaling:
            nodes = manager.get_node_metadatas(AWS_RUNNING_STATES)
            offset = worker_setup.prescaling.of(len(nodes))
            logger.info(f"Applying pre-scaling of {offset} node to {migration_event.cluster}:{migration_event.pool}")
            avg_weight = mean(node.instance.weight for node in nodes)
            prescaled_capacity = round(manager.target_capacity + (offset * avg_weight))
            manager.modify_target_capacity(prescaled_capacity)
        if not _monitor_pool_health(
            manager=manager,
            timeout=time.time() + INITIAL_POOL_HEALTH_TIMEOUT_SECONDS,
            drained=[],
            health_check_interval_seconds=worker_setup.health_check_interval,
            ignore_pod_health=True,
        ):
            raise NodeMigrationError(f"Pool {migration_event.cluster}:{migration_event.pool} is not healthy")
        node_selector = lambda node: node.agent.agent_id and not migration_event.condition.matches(node)  # noqa
        migration_routine = partial(_drain_node_selection, manager, node_selector, worker_setup)
        if not limit_function_runtime(migration_routine, worker_setup.expected_duration):
            raise NodeMigrationError(f"Failed migrating nodes for event {migration_event}")
    except Exception as e:
        logger.error(f"Issue while processing migration event {migration_event}: {e}")
        raise
    finally:
        if pool_lock_acquired:
            pool_lock.release()
        # we do not reset the pool target capacity in case of pre-scaling as we
        # trust the autoscaler to readjust that in a short time eventually
        if worker_setup.disable_autoscaling:
            logger.info(f"Re-enabling autoscaling for {migration_event.cluster}:{migration_event.pool}")
            enable_autoscaling(migration_event.cluster, migration_event.pool, SUPPORTED_POOL_SCHEDULER)
