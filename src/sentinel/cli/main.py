"""`sentinel` 命令行（Typer）。

负责：命令的入口和参数解析。各子命令（patrol、bench、eval、version、approvals……）按 08 篇的排期逐天加入。
不负责：业务逻辑，一律转给 core / eval / governance。
"""

import typer

app = typer.Typer(no_args_is_help=True, help="SentinelOps 运维 Agent")


@app.command()
def info() -> None:
    """打印模型配置里有哪些用途和对应型号（型号只来自配置文件，ADR-008）。"""
    from sentinel.core.llm.config import load_models_config

    for purpose, m in sorted(load_models_config().purposes.items()):
        typer.echo(f"{purpose}: {m.provider}/{m.model}")


@app.callback()
def main() -> None:
    """有了 callback，Typer 才会把 info 当作子命令（而不是唯一的默认命令）。"""
