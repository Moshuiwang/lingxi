"""随包发布的可维护配置文件。

放在包内而不是仓库根目录，是为了让 `pip install` 出来的制品一定带着它：
配置文件缺失会让职能标签整列变成「未映射」，而那种失败在部署时才暴露。
打包声明见 `pyproject.toml` 的 `[tool.setuptools.package-data]`，
CI 用 `scripts/ci/check_installed_package.py` 校验它确实进了制品。
"""
