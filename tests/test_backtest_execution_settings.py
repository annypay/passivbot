import argparse
from copy import deepcopy
import json

import pytest

from backtest import get_backtest_execution_settings
from config import (
    compile_runtime_config,
    get_template_config,
    load_prepared_config,
    prepare_config,
    project_config,
)
from config_utils import (
    add_config_arguments,
    project_template_config_for_cli,
    strip_config_metadata,
    update_config_with_args,
)
from optimization.backends.gpu_backend import validate_gpu_preparation_scope
from optimization.gpu.service import (
    MpsMulticoinProxy,
    MpsSingleCoinProxy,
    validate_gpu_execution_settings,
)


DEFAULT_EXECUTION = {
    "execution_delay_bars": 0,
    "intrabar_fill_order": "close_first",
    "execution_audit_path": None,
}
CUSTOM_EXECUTION = {
    "execution_delay_bars": 1,
    "intrabar_fill_order": "entry_first",
    "execution_audit_path": "results/execution.csv",
}


def _execution_values(config):
    return {key: config["backtest"][key] for key in DEFAULT_EXECUTION}


@pytest.mark.parametrize("wrapped", [False, True])
def test_legacy_execution_settings_hydrate_through_prepare_and_load(tmp_path, wrapped):
    source = get_template_config()
    for key in DEFAULT_EXECUTION:
        del source["backtest"][key]
    if wrapped:
        source = {"config": source}
    original = deepcopy(source)
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(source))

    prepared = prepare_config(source, verbose=False)
    loaded = load_prepared_config(str(path), verbose=False, log_info=False)

    assert source == original
    assert _execution_values(get_template_config()) == DEFAULT_EXECUTION
    assert _execution_values(prepared) == DEFAULT_EXECUTION
    assert _execution_values(loaded) == DEFAULT_EXECUTION
    assert _execution_values(
        load_prepared_config(None, verbose=False, log_info=False)
    ) == DEFAULT_EXECUTION


@pytest.mark.parametrize("runtime", ["backtest", "optimize"])
@pytest.mark.parametrize("values", [DEFAULT_EXECUTION, CUSTOM_EXECUTION])
def test_execution_settings_roundtrip_and_runtime_compilation(tmp_path, runtime, values):
    source = get_template_config()
    source["backtest"].update(values)
    prepared = prepare_config(source, verbose=False)
    compiled = compile_runtime_config(prepared, runtime=runtime)
    settings = get_backtest_execution_settings(compiled, is_runtime_compiled=True)
    path = tmp_path / "roundtrip.json"
    path.write_text(json.dumps(strip_config_metadata(prepared)))
    loaded = load_prepared_config(
        str(path), target=runtime, runtime=runtime, verbose=False, log_info=False
    )

    for config in (prepared, compiled, loaded):
        assert _execution_values(config) == values
    for key, value in values.items():
        assert getattr(settings, key) == value
    assert compiled["bot"] == compile_runtime_config(
        prepare_config(get_template_config(), verbose=False), runtime=runtime
    )["bot"]
    assert not any(key in json.dumps(prepared["optimize"]["bounds"]) for key in values)


@pytest.mark.parametrize(
    "key,value,error_type",
    [
        *[
            ("execution_delay_bars", value, TypeError)
            for value in (True, False, None, 1.0, 1.5, "1", [], float("nan"), float("inf"))
        ],
        ("execution_delay_bars", -1, ValueError),
        *[
            ("intrabar_fill_order", value, ValueError)
            for value in ("", "ohlc", "Close_First", "entry_first ", None, 0, [], {})
        ],
        *[
            ("execution_audit_path", value, ValueError)
            for value in ("", "   ", True, False, 0, [], {})
        ],
    ],
)
def test_execution_settings_reject_invalid_config_and_payload_values(key, value, error_type):
    source = get_template_config()
    source["backtest"][key] = value

    with pytest.raises(error_type, match=key):
        prepare_config(source, verbose=False)
    with pytest.raises(error_type, match=key):
        get_backtest_execution_settings(source)


@pytest.mark.parametrize("key", DEFAULT_EXECUTION)
def test_compiled_execution_settings_require_canonical_fields(key):
    compiled = compile_runtime_config(get_template_config(), runtime="backtest")
    del compiled["backtest"][key]

    with pytest.raises(KeyError, match=key):
        get_backtest_execution_settings(compiled, is_runtime_compiled=True)


@pytest.mark.parametrize("command", ["backtest", "optimize"])
@pytest.mark.parametrize("flag_style", ["friendly", "dotted", "underscored"])
def test_execution_cli_roundtrip_and_help(command, flag_style):
    parser = argparse.ArgumentParser()
    template = project_template_config_for_cli(get_template_config(), command)
    allowed_keys = add_config_arguments(parser, template, command=command)
    argv = []
    for key, value in CUSTOM_EXECUTION.items():
        flag = {
            "friendly": "--" + key.replace("_", "-"),
            "dotted": "--backtest." + key,
            "underscored": "--backtest_" + key,
        }[flag_style]
        argv.extend([flag, str(value)])
    source = get_template_config()
    update_config_with_args(source, parser.parse_args(argv), allowed_keys=allowed_keys)

    prepared = prepare_config(source, target=command, runtime=command, verbose=False)

    assert _execution_values(prepared) == CUSTOM_EXECUTION
    assert type(prepared["backtest"]["execution_delay_bars"]) is int
    help_text = parser.format_help()
    assert "T+1" in help_text and "T+2" in help_text
    assert "OHLC path" in help_text
    assert "--execution-audit-path" in help_text


@pytest.mark.parametrize("value", ["true", "1.5", "1.0", "nan"])
def test_execution_delay_cli_does_not_coerce_nonintegers(value):
    parser = argparse.ArgumentParser()
    add_config_arguments(parser, get_template_config(), command="backtest")

    with pytest.raises(SystemExit):
        parser.parse_args(["--execution-delay-bars", value])


def test_simulation_settings_do_not_enter_live_projection_or_cli():
    source = get_template_config()
    source["backtest"].update(CUSTOM_EXECUTION)
    source["optimize"]["backend"] = "gpu"
    prepared = prepare_config(source, live_only=True, verbose=False)
    live = compile_runtime_config(project_config(prepared, "live"), runtime="live")

    assert "backtest" not in live
    assert not any(key in live["live"] for key in DEFAULT_EXECUTION)
    parser = argparse.ArgumentParser()
    allowed_keys = add_config_arguments(
        parser,
        project_config(get_template_config(), "live"),
        command="live",
    )
    assert not any(f"backtest.{key}" in allowed_keys for key in DEFAULT_EXECUTION)
    assert "--execution-delay-bars" not in parser.format_help()


@pytest.mark.parametrize("key,value", CUSTOM_EXECUTION.items())
def test_gpu_preparation_rejects_unmodeled_execution_before_runtime_setup(monkeypatch, key, value):
    source = get_template_config()
    source["backtest"][key] = value

    def unexpected_runtime(*args, **kwargs):
        pytest.fail("GPU runtime setup must not start for unsupported execution settings")

    monkeypatch.setattr("optimization.gpu.runtime.gpu_device", unexpected_runtime)
    with pytest.raises(ValueError, match=f"backtest.{key}"):
        validate_gpu_preparation_scope(source)


@pytest.mark.parametrize("key,value", CUSTOM_EXECUTION.items())
def test_gpu_suite_rejects_unmodeled_execution_overrides_before_runtime_setup(
    monkeypatch, key, value
):
    source = get_template_config()
    suite = {
        "enabled": True,
        "scenarios": [
            {"label": "execution", "overrides": {f"backtest.{key}": value}}
        ],
    }

    def unexpected_runtime(*args, **kwargs):
        pytest.fail("GPU runtime setup must not run")

    monkeypatch.setattr("optimization.gpu.runtime.gpu_device", unexpected_runtime)
    with pytest.raises(ValueError, match=f"backtest.{key}"):
        validate_gpu_preparation_scope(source, suite)


@pytest.mark.parametrize("proxy_cls", [MpsSingleCoinProxy, MpsMulticoinProxy])
@pytest.mark.parametrize("key,value", CUSTOM_EXECUTION.items())
def test_proxy_rejects_unmodeled_execution_before_data_or_device_work(
    monkeypatch, proxy_cls, key, value
):
    source = get_template_config()
    source["backtest"][key] = value

    def unexpected_runtime(*args, **kwargs):
        pytest.fail("GPU device initialization must not run")

    monkeypatch.setattr("optimization.gpu.service.gpu_device", unexpected_runtime)
    with pytest.raises(ValueError, match=f"backtest.{key}"):
        proxy_cls(
            config=source,
            hlcvs=None,
            mss={},
            btc=None,
            timestamps=None,
            exchange="binance",
            batch_size=1,
            needed_metrics=set(),
        )


def test_gpu_accepts_canonical_default_execution_settings():
    validate_gpu_execution_settings(get_template_config())
