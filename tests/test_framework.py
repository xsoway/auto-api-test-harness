"""Step 2 验证：日志与配置能正确加载，且对隔离运行安全。"""
import os

from ah.core.config import load_settings
from ah.utils.logging import configure_logging, get_logger


def test_settings_load_and_env_override(tmp_path):
    os.environ["AH_BASE_URL"] = "http://env.invalid"
    try:
        cfg = tmp_path / "config.yaml"
        cfg.write_text("settings:\n  base_url: http://file.invalid\n")
        s = load_settings(str(cfg))  # 新的缓存条目会重新读取环境变量
        assert s.base_url == "http://env.invalid", "env should win over file"
        assert s.llm.model_cheap  # 存在轻量模型默认值
    finally:
        os.environ.pop("AH_BASE_URL", None)


def test_config_path_anchored_at_project_root():
    """默认配置路径按 __file__ 定位到项目根 config/config.yaml（任意 cwd 可寻址）。"""
    import ah.core.config as cfgmod

    default = cfgmod._default_config_path()
    # 绝对路径 + 指向项目根的 config/config.yaml，且文件确实存在。
    assert default.startswith("/")
    assert default.endswith("/config/config.yaml")
    assert __import__("pathlib").Path(default).is_file()
    # 与 __file__ 所在仓库根一致（src/ah/core/config.py 上探三级到 src 的上级）。
    expected_root = __import__("pathlib").Path(cfgmod.__file__).resolve().parents[3]
    assert __import__("pathlib").Path(default).parent == expected_root / "config"


def test_logger_configured_under_root(tmp_path):
    import logging as _logging

    root = _logging.getLogger("ah")
    root._ah_configured = False  # type: ignore[attr-defined]  # 重置全局守卫
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    configure_logging(log_dir=tmp_path / "logs")
    logger = get_logger("client")
    assert logger.name == "ah.client"
    logger.info("smoke %s", "ok")
    log_file = tmp_path / "logs" / "ah.log"
    assert log_file.is_file()
    assert "smoke ok" in log_file.read_text()
