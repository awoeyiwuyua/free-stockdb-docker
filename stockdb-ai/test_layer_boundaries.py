"""test_layer_boundaries — 四层架构依赖纪律物理检查（0.9.1 框架核心交付；0.9.8 接口层收拢）。

用 ast 静态扫描各层包内 import，断言依赖方向（设计文档 docs/design/application-layer.md §3）：
  - core/（领域层）：只允许 config 与 stdlib —— 不依赖任何其他层
  - services/：禁止依赖接口层（interfaces/）
  - storage/：禁止依赖 services/ core/ interfaces/
  - ops/：禁止依赖 interfaces/ services/ core/
  - interfaces/（接口层）：允许依赖一切下层（services/core/storage/ops/config）
  - config：所有层可用（纯 stdlib）

0.9.8 严格分层：接口层统一收拢为 interfaces/（web/ HTTP + mcp/ MCP），
依赖铁律按接口层整体检查。违规即测试红——"墙"的物理保证，防止未来代码越界。
"""
import ast
import pathlib
import unittest

WEBUI = pathlib.Path(__file__).resolve().parent

# 层 → 允许 import 的层（含 config；stdlib 天然允许）
ALLOWED = {
    "interfaces": {"interfaces", "services", "core", "storage", "ops", "config"},
    "services": {"services", "core", "storage", "ops", "config"},
    "core": {"core", "config"},  # 领域层最严：纯规则，零外部依赖
    "storage": {"storage", "ops", "config"},
    "ops": {"ops", "storage", "config"},
}
LAYER_PACKAGES = ("interfaces", "services", "core", "storage", "ops")
# 0.9.8：接口层整体禁止被下层依赖（web/mcp 已收拢 interfaces/，无需再单列）
FORBIDDEN = {
    "services": {"interfaces", "storage.providers"},  # 0.9.5 M5：服务层只依赖注入的仓储接口
    "storage": {"interfaces", "services", "core"},
    "ops": {"interfaces", "services", "core"},
    "core": {"interfaces", "services", "storage", "ops"},
}


def scan_imports(source: str) -> list[str]:
    """解析源码 → import 的完整模块名列表（绝对导入；相对导入推导目标包）。"""
    tree = ast.parse(source)
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.level == 0:
                out.append(node.module)
            else:
                # 相对导入：level=1 指向本包；level=2 指向父包（webui 根，非层）
                parts = node.module.split(".") if node.module else []
                if node.level == 1 and parts:
                    out.append(parts[0])
    return out


def check_source(source: str, layer: str) -> list[str]:
    """单文件依赖检查：返回违规 import 描述列表（空 = 合规）。

    匹配规则：先按首段包名查 ALLOWED/FORBIDDEN；完整名命中 FORBIDDEN
    （如 services 禁 storage.providers）同样违规。
    """
    violations = []
    for full in scan_imports(source):
        pkg = full.split(".")[0]
        if pkg == layer or pkg in ALLOWED.get(layer, set()):
            continue
        if (pkg in LAYER_PACKAGES or pkg in FORBIDDEN.get(layer, set())
                or full in FORBIDDEN.get(layer, set())):
            violations.append(f"{layer}/ 越界 import {full}")
    return violations


def check_package(pkg_dir: pathlib.Path, layer: str) -> list[str]:
    """包内全部 .py 的依赖检查（递归）。"""
    violations = []
    for py in sorted(pkg_dir.rglob("*.py")):
        rel = py.relative_to(pkg_dir.parent)
        try:
            src = py.read_text(encoding="utf-8")
        except OSError:
            continue
        for v in check_source(src, layer):
            violations.append(f"{rel}: {v}")
    return violations


class LayerBoundaryTest(unittest.TestCase):
    """四层包真实代码的依赖纪律（0.9.1 起每层一测，违规即红）。"""

    def _assert_layer_clean(self, layer: str):
        pkg = WEBUI / layer
        if not pkg.is_dir():
            self.skipTest(f"{layer}/ 尚未创建")
        violations = check_package(pkg, layer)
        self.assertEqual(violations, [], "\n".join(violations))

    def test_interfaces_clean(self):
        self._assert_layer_clean("interfaces")

    def test_services_clean(self):
        self._assert_layer_clean("services")

    def test_core_clean(self):
        self._assert_layer_clean("core")

    def test_storage_clean(self):
        self._assert_layer_clean("storage")

    def test_ops_clean(self):
        self._assert_layer_clean("ops")

    def test_core_cannot_import_services(self):
        """领域层 import 服务层 → 违规（检查器有效性验证）。"""
        src = "import services\nfrom storage import x\nimport json\nimport config"
        v = check_source(src, "core")
        self.assertIn("core/ 越界 import services", v)
        self.assertIn("core/ 越界 import storage", v)
        self.assertNotIn("config", " ".join(v))  # config 允许

    def test_services_cannot_import_interfaces(self):
        src = ("from interfaces.web import routes\n"
               "from interfaces.mcp import stockdb_mcp_server\nimport ops")
        v = check_source(src, "services")
        self.assertIn("services/ 越界 import interfaces.web", v)
        self.assertIn("services/ 越界 import interfaces.mcp", v)
        self.assertNotIn("ops", " ".join(v))

    def test_storage_cannot_import_core(self):
        src = "from core import board_metrics\nimport ops\nimport config"
        v = check_source(src, "storage")
        self.assertIn("storage/ 越界 import core", v)
        self.assertNotIn("ops", " ".join(v))
        self.assertNotIn("config", " ".join(v))


if __name__ == "__main__":
    unittest.main(verbosity=2)
