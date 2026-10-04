from kf_oj.workers.bootstrap import check_entrypoint


def main() -> None:
    check_entrypoint("outbox-publisher")


if __name__ == "__main__":
    main()
