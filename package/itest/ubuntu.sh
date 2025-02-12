#!/bin/bash

set -exo pipefail

cd /

highlight() {
  echo -n "$(tput setaf 3)"
  echo -n "$@"
  echo "$(tput op)"
}

highlight_exec() {
  highlight "$@"
  command "$@"
  return $?
}

PACKAGE_NAME="$1"
PACKAGE_VERSION="$2"
PAASTA_ENV="$3"

# This will get DISTRIB_CODENAME
source /etc/lsb-release
export DISTRIB_CODENAME
echo $PACKAGE_NAME $PACKAGE_VERSION $DISTRIB_CODENAME $EXAMPLE

# Set up the timezone so clusterman_metrics gets the right data
export TZ=US/Pacific
ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

apt-get update && apt-get install -y software-properties-common
# we really only need this externally, but we use a python not included
# by ubuntu - so add the deadsnakes ppa to bring that in
if [ "${PAASTA_ENV}" != "YELP" ]; then
    add-apt-repository ppa:deadsnakes/ppa
fi
# our debian/control will already install py3.7, but we want to install it ahead of time so that
# we can also get the right pip version installed as well.
apt-get install -y --force-yes python3.7 python3-pip python3-yaml
# Install package directly with any needed dependencies

# we also install python3-distutils here to avoid issues on newer ubuntus
# where disutils isn't included with python (and even though clusterman depends on it, the right
# version isn't installed in this itest container)
if  [ "${DISTRIB_CODENAME}" != "xenial" ]; then
apt-get install -y --force-yes python3.7-distutils
fi

apt-get install -y --force-yes ./dist/${DISTRIB_CODENAME}/clusterman_${PACKAGE_VERSION}_amd64.deb

# Sometimes our acceptance tests run in parallel on the same box, so we need to use different CIDR ranges
if [ "${DISTRIB_CODENAME}" = "xenial" ]; then
    CIDR_BLOCK="10.0.0.0/24"
else
    CIDR_BLOCK="10.1.0.0/24"
fi

export ACCEPTANCE_ROOT=/itest
python3.7 -m pip install boto3 simplejson pyyaml
python3.7 /itest/run_instance.py \
    http://moto-ec2:5000/ \
    http://moto-s3:5000/ \
    http://moto-dynamodb:5000/ \
    "${CIDR_BLOCK}"

# Run the critical clusterman CLI commands
if [ ! "${EXAMPLE}" ]; then
    highlight_exec /usr/bin/clusterman --version
    highlight_exec /usr/bin/clusterman status --cluster local-dev -v
    highlight_exec /usr/bin/clusterman manage --cluster local-dev --target-capacity 10 --dry-run
    highlight_exec /usr/bin/clusterman disable --cluster local-dev --until tomorrow
    highlight_exec /usr/bin/clusterman enable --cluster local-dev
    highlight_exec /usr/bin/clusterman simulate --cluster local-dev --start-time 2017-12-01T08:00:00Z --end-time 2017-12-01T09:00:00Z --metrics-data-files /itest/metrics.json.gz
    highlight_exec /usr/bin/clusterman --log-level debug simulate --cluster local-dev --scheduler mesos --autoscaler-config /itest/autoscaler_config.yaml --start-time 2017-12-01T08:00:00Z --end-time 2017-12-01T08:05:00Z --metrics-data-files /itest/metrics.json.gz

    highlight "$0:" 'success!'
else
    /bin/bash
fi
