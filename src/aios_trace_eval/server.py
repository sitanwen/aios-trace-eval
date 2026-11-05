from __future__ import annotations

import os


def main() -> None:
    import uvicorn
    uvicorn.run(
        "aios_trace_eval.app:app",
        host=os.environ.get("AIOS_HOST", "0.0.0.0"),
        port=int(os.environ.get("AIOS_PORT", "8080")),
        reload=os.environ.get("AIOS_RELOAD", "0") == "1",
    )


if __name__ == "__main__":
    main()
