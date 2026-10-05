"""Probe 이름 변경 뒤 자격 증명과 실행 경로의 호환성을 확인한다."""
from pathlib import Path
import xml.etree.ElementTree as ET

from probe.control_plane import Credentials
from probe.secret_store import WindowsCredentialStore


def test_probe_store_prefers_new_key_and_reads_legacy_without_copying(monkeypatch):
    store = WindowsCredentialStore()
    assert store.namespace == "Probe"
    values = {("H-TRSA", "API_KEY"): ("saved-key", "legacy-date")}
    calls = []
    def read(name, namespace):
        calls.append((namespace, name))
        return values.get((namespace, name), (None, None))
    monkeypatch.setattr(store, "_read_namespace", read)
    assert store.read("API_KEY") == ("saved-key", "legacy-date")
    assert calls == [("Probe", "API_KEY"), ("H-TRSA", "API_KEY")]
    values[("Probe", "API_KEY")] = ("new-key", "new-date")
    calls.clear()
    assert store.read("API_KEY") == ("new-key", "new-date")
    assert calls == [("Probe", "API_KEY")]


def test_delete_removes_both_namespaces_to_prevent_key_reactivation(monkeypatch):
    store = WindowsCredentialStore()
    deleted = []
    class NativeStore:
        def CredDeleteW(self, target, *_args):
            deleted.append(target)
            return True
    monkeypatch.setattr(store, "dll", NativeStore(), raising=False)
    store.available = True
    store.write("API_KEY", None)
    assert deleted == ["Probe/API_KEY", "H-TRSA/API_KEY"]


def test_protected_legacy_file_is_retained_without_rewriting(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    legacy = tmp_path / "H-TRSA" / "secrets.env"
    legacy.parent.mkdir()
    content = "API_KEY=saved-key\n"
    legacy.write_bytes(content.encode("utf-8", errors="strict"))
    credentials = Credentials(tmp_path / "repository", tmp_path / "workspace")
    assert credentials.file == legacy
    assert legacy.read_bytes() == content.encode("utf-8", errors="strict")
    new_file = tmp_path / "Probe" / "secrets.env"
    new_file.parent.mkdir()
    new_file.write_bytes(b"API_KEY=new-key\n")
    assert Credentials(tmp_path / "repository", tmp_path / "workspace").file == new_file


def test_launchers_and_distribution_use_probe():
    root = Path(__file__).resolve().parents[1]
    launcher = root / "Probe.wsf"
    assert "-m probe.desktop" in launcher.read_text(encoding="utf-8")
    assert ET.parse(launcher).getroot().attrib["id"] == "PROBE"
    assert ET.parse(root / "Probe-과학검증.wsf").getroot().attrib["id"] == "PROBEScienceValidation"
    assert not (root / "src" / "htrsa").exists()
    import tomllib
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["project"]["name"] == "probe"
    assert "probe" in config["tool"]["setuptools"]["package-data"]


def test_legacy_and_probe_model_environment_are_not_exported():
    from probe.release import _secret_free
    assert not _secret_free("settings.txt", b"PROBE_MANAGER_MODEL=private-model")
    assert not _secret_free("settings.txt", b"HTRSA_MANAGER_MODEL=private-model")
