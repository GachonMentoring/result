"""K8s 접속 설정 로딩: 파드 안이면 ServiceAccount, 아니면 로컬 kubeconfig."""

import logging

from kubernetes import config

log = logging.getLogger(__name__)


def load_kube_config() -> str:
    try:
        config.load_incluster_config()
        log.info("in-cluster 설정(ServiceAccount)으로 접속합니다.")
        return "in-cluster"
    except config.ConfigException:
        config.load_kube_config()
        log.info("로컬 kubeconfig로 접속합니다.")
        return "kubeconfig"
