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
from datetime import timedelta
from itertools import chain
from itertools import repeat
from unittest.mock import call
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from clusterman.interfaces.types import AgentMetadata
from clusterman.interfaces.types import ClusterNodeMetadata
from clusterman.interfaces.types import InstanceMetadata
from clusterman.migration.settings import MigrationPrecendence
from clusterman.migration.settings import PoolPortion
from clusterman.migration.settings import WorkerSetup
from clusterman.migration.worker import _drain_node_selection
from clusterman.migration.worker import _monitor_pool_health
from clusterman.migration.worker import RestartableDaemonProcess
from clusterman.migration.worker import uptime_migration_worker


@patch("clusterman.migration.worker.time")
def test_monitor_pool_health(mock_time):
    mock_manager = MagicMock()
    mock_connector = mock_manager.cluster_connector
    drained = [
        ClusterNodeMetadata(
            AgentMetadata(agent_id=i), InstanceMetadata(market=None, weight=None, ip_address=f"{i}.{i}.{i}.{i}")
        )
        for i in range(5)
    ]
    mock_manager.is_capacity_satisfied.side_effect = [False, True, True]
    mock_connector.get_unschedulable_pods.side_effect = [True, False]
    mock_connector.get_agent_metadata.side_effect = chain(
        (AgentMetadata(agent_id=i) for i in range(3)),
        repeat(AgentMetadata(agent_id="")),
    )
    mock_time.time.return_value = 0
    assert _monitor_pool_health(mock_manager, 1, drained) is True
    # 1st iteration still draining some nodes
    # 2nd iteration underprovisioned capacity
    # 3rd iteration left over unscheduable pods
    assert mock_time.sleep.call_count == 3


@patch("clusterman.migration.worker.time")
@patch("clusterman.migration.worker._monitor_pool_health")
def test_drain_node_selection(mock_monitor, mock_time):
    mock_manager = MagicMock()
    mock_monitor.return_value = True
    mock_manager.get_node_metadatas.return_value = [
        ClusterNodeMetadata(AgentMetadata(agent_id=i, task_count=30 - 2 * i), InstanceMetadata(None, None))
        for i in range(6)
    ]
    mock_time.time.side_effect = range(5)
    worker_setup = WorkerSetup(
        rate=PoolPortion(2),
        prescaling=None,
        precedence=MigrationPrecendence.TASK_COUNT,
        bootstrap_wait=1,
        bootstrap_timeout=2,
        disable_autoscaling=False,
        expected_duration=3,
    )
    assert _drain_node_selection(mock_manager, lambda n: n.agent.agent_id > 2, worker_setup) is True
    mock_manager.get_node_metadatas.assert_called_once_with(("running",))
    mock_manager.submit_for_draining.assert_has_calls(
        [
            call(ClusterNodeMetadata(AgentMetadata(agent_id=i, task_count=30 - 2 * i), InstanceMetadata(None, None)))
            for i in range(5, 2, -1)
        ]
    )
    mock_monitor.assert_has_calls(
        [
            call(
                mock_manager,
                2,
                [
                    ClusterNodeMetadata(AgentMetadata(agent_id=5, task_count=20), InstanceMetadata(None, None)),
                    ClusterNodeMetadata(AgentMetadata(agent_id=4, task_count=22), InstanceMetadata(None, None)),
                ],
            ),
            call(
                mock_manager,
                3,
                [
                    ClusterNodeMetadata(AgentMetadata(agent_id=3, task_count=24), InstanceMetadata(None, None)),
                ],
            ),
        ]
    )


@patch("clusterman.migration.worker.time")
@patch("clusterman.migration.worker.PoolManager")
@patch("clusterman.migration.worker._drain_node_selection")
def test_uptime_migration_worker(mock_drain_selection, mock_manager_class, mock_time):
    mock_setup = MagicMock()
    mock_manager = mock_manager_class.return_value
    mock_manager.is_capacity_satisfied.side_effect = [True, False, True]
    with pytest.raises(StopIteration):  # using end of mock side-effect to get out of forever looop
        uptime_migration_worker("mesos-test", "bar", 10000, mock_setup)
    assert mock_drain_selection.call_count == 2
    selector = mock_drain_selection.call_args_list[0][0][1]
    assert selector(ClusterNodeMetadata(None, InstanceMetadata(None, None, uptime=timedelta(seconds=10001)))) is True
    assert selector(ClusterNodeMetadata(None, InstanceMetadata(None, None, uptime=timedelta(seconds=9999)))) is False


def test_restartable_daemon_process():
    proc = RestartableDaemonProcess(lambda: time.sleep(10), tuple(), {})
    proc.start()
    time.sleep(0.05)
    assert proc.is_alive()
    old_handle = proc.process_handle
    proc.restart()
    time.sleep(0.05)
    assert proc.is_alive()
    assert proc.process_handle is not old_handle
    proc.kill()