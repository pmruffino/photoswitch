import asyncio
import os
import sys


def main() -> None:
    worker_type = os.environ.get("WORKER_TYPE", "").lower()

    if worker_type == "fetcher":
        from fetcher import FetcherWorker
        worker = FetcherWorker()
    elif worker_type == "unpacker":
        from unpacker import UnpackerWorker
        worker = UnpackerWorker()
    elif worker_type == "mapper":
        from mapper.mapper import MapperWorker
        worker = MapperWorker()
    elif worker_type == "loader":
        from loader import LoaderWorker
        worker = LoaderWorker()
    elif worker_type == "rollback":
        from rollback import RollbackWorker
        worker = RollbackWorker()
    else:
        print(f"Unknown WORKER_TYPE: {worker_type!r}. Must be fetcher, unpacker, mapper, loader, or rollback.", file=sys.stderr)
        sys.exit(1)

    asyncio.run(worker.run())


if __name__ == "__main__":
    main()
