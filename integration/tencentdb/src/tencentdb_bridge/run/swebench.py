"""SWE-bench run factory for the tencentdb arm (the stock runner with the
memory agent rebound in)."""

from shared_bridge.run import bind_swebench_app

from tencentdb_bridge.agent import TencentDBAgent

app = bind_swebench_app(TencentDBAgent)

if __name__ == "__main__":
    app()
