"""Fixed broker and storage bounds shared by setup and read-only observation."""

QUEUE = "kf.judge.evaluations.v1"
QUEUE_ARGUMENTS = {
    "x-max-length": 64,
    "x-max-length-bytes": 32768,
    "x-overflow": "reject-publish",
    "x-message-ttl": 600000,
}
STORAGE = {
    "postgres": {
        "path": "/var/lib/kf-onlinejudge/postgres",
        "filesystem_bytes": 2 * 1024**3,
    },
    "rabbitmq": {
        "path": "/var/lib/kf-onlinejudge/rabbitmq",
        "filesystem_bytes": 512 * 1024**2,
    },
}
