"""The installer chart's fetch step: what it verifies before Calibre runs the zip.

The checks render the real template with ``helm template`` and then run the
rendered shell script against a stub ``curl``, so the verification logic is
exercised rather than only grepped for. Locally they are skipped when helm is
not on PATH. With ``CI`` set they fail instead, so a runner without helm cannot
turn them into a silent pass; the workflow installs helm for them.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
import stat
import subprocess
import textwrap

import pytest

CHART = pathlib.Path(__file__).resolve().parent.parent / "charts" / "calibre-plugin-installer"
ZIP_NAME = "calibre-bridge-v0.8.3.zip"
ZIP_BYTES = b"pretend zip bytes"
ZIP_SHA = hashlib.sha256(ZIP_BYTES).hexdigest()


def _need_helm() -> None:
    if shutil.which("helm") is not None:
        return
    if os.environ.get("CI"):
        pytest.fail("helm is not installed, and CI must run the rendered chart tests")
    pytest.skip("helm is not installed")


@pytest.fixture
def require_helm():
    _need_helm()


needs_helm = pytest.mark.usefixtures("require_helm")


def _render(*sets: str) -> str:
    args = ["helm", "template", "ci", str(CHART)]
    for item in sets:
        args += ["--set", item]
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def _fetch_script(rendered: str) -> str:
    start = rendered.index("              set -eu\n              cd /bindery-stage")
    end = rendered.index("          securityContext:", start)
    return textwrap.dedent(rendered[start:end])


# the template text, no helm needed


def test_template_restricts_curl_to_https():
    text = (CHART / "templates" / "patch.yaml").read_text()
    assert text.count("--proto '=https' --proto-redir '=https'") == 2


def test_both_init_containers_set_the_runtime_default_seccomp_profile():
    text = (CHART / "templates" / "patch.yaml").read_text()
    assert text.count("seccompProfile:\n              type: RuntimeDefault") == 2


def test_the_default_pin_is_empty():
    assert 'pluginSha256: ""' in (CHART / "values.yaml").read_text()


# rendered


@needs_helm
def test_unset_pin_renders_an_empty_hash_and_keeps_the_sidecar_check():
    out = _render()
    assert 'name: PLUGIN_SHA256\n              value: ""' in out
    script = _fetch_script(out)
    assert "sidecar_name" in script
    assert "--proto '=https' --proto-redir '=https'" in script


@needs_helm
def test_set_pin_is_rendered_lower_cased_and_checked():
    out = _render(f"pluginSha256={ZIP_SHA.upper()}")
    assert f'value: "{ZIP_SHA}"' in out
    script = _fetch_script(out)
    assert 'sha256sum "$PLUGIN_ZIP"' in script
    assert '"$actual" != "$PLUGIN_SHA256"' in script


@needs_helm
@pytest.mark.parametrize("bad", ["abc", "z" * 64, ZIP_SHA + "0"])
def test_a_malformed_pin_fails_the_render(bad):
    with pytest.raises(subprocess.CalledProcessError) as err:
        _render(f"pluginSha256={bad}")
    assert "pluginSha256 must be 64 hex digits" in err.value.stderr


@needs_helm
def test_both_init_containers_render_the_seccomp_profile():
    assert _render().count("type: RuntimeDefault") == 2


# the rendered script, run against a stub curl


def _run(tmp_path, script, pin="", verify="true", sidecar=None, zip_bytes=ZIP_BYTES):
    """Run the fetch script. Returns (exit code, output, curl argument lines)."""
    served = tmp_path / "served"
    served.mkdir()
    (served / ZIP_NAME).write_bytes(zip_bytes)
    side = f"{ZIP_SHA}  {ZIP_NAME}\n" if sidecar is None else sidecar
    (served / f"{ZIP_NAME}.sha256").write_text(side)
    stage = tmp_path / "stage"
    stage.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "curl.log"
    curl = bindir / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        'echo "$*" >> "$CURL_LOG"\n'
        'while [ $# -gt 0 ]; do case "$1" in -o) out=$2;; http*) url=$1;; esac; shift; done\n'
        'cp "$SERVED/$(basename "$url")" "$out"\n'
    )
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    # The script cds to /bindery-stage; point that at the temp stage dir.
    script = script.replace("cd /bindery-stage", f"cd {stage}")
    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "CURL_LOG": str(log),
        "SERVED": str(served),
        "PLUGIN_VERSION": "0.8.3",
        "PLUGIN_URL": f"https://example.invalid/{ZIP_NAME}",
        "PLUGIN_ZIP": ZIP_NAME,
        "VERIFY_CHECKSUM": verify,
        "PLUGIN_SHA256": pin,
    }
    proc = subprocess.run(
        ["sh", "-c", script], env=env, capture_output=True, text=True, check=False
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return proc.returncode, proc.stdout + proc.stderr, calls


@pytest.fixture(scope="module")
def script():
    _need_helm()
    return _fetch_script(_render())


@needs_helm
def test_a_matching_sidecar_passes(tmp_path, script):
    code, out, calls = _run(tmp_path, script)
    assert code == 0, out
    assert "Checksum verified" in out
    assert len(calls) == 2
    assert all("--proto =https --proto-redir =https" in c for c in calls)


@needs_helm
def test_a_sidecar_naming_another_file_fails(tmp_path, script):
    """sha256sum -c alone passes this: it checks whichever file the sidecar names."""
    other = f"{ZIP_SHA}  something-else.zip\n"
    code, out, _ = _run(tmp_path, script, sidecar=other)
    assert code != 0
    assert "names a different file" in out


@needs_helm
def test_a_sidecar_with_a_second_line_fails(tmp_path, script):
    code, out, _ = _run(tmp_path, script, sidecar=f"{ZIP_SHA}  {ZIP_NAME}\n{ZIP_SHA}  other.zip\n")
    assert code != 0
    assert "not a single line" in out


@needs_helm
def test_a_sidecar_for_other_bytes_fails(tmp_path, script):
    code, _out, _ = _run(tmp_path, script, sidecar=f"{'0' * 64}  {ZIP_NAME}\n")
    assert code != 0


@needs_helm
def test_a_sidecar_without_a_final_newline_still_passes(tmp_path, script):
    code, out, _ = _run(tmp_path, script, sidecar=f"{ZIP_SHA}  {ZIP_NAME}")
    assert code == 0, out


@needs_helm
def test_a_matching_pin_passes(tmp_path, script):
    code, out, _ = _run(tmp_path, script, pin=ZIP_SHA)
    assert code == 0, out
    assert "pluginSha256 matched" in out


@needs_helm
def test_a_wrong_pin_fails_even_when_the_sidecar_agrees(tmp_path, script):
    """A swapped zip with a matching swapped sidecar is exactly what the pin stops."""
    swapped = b"malicious zip"
    sidecar = f"{hashlib.sha256(swapped).hexdigest()}  {ZIP_NAME}\n"
    code, out, calls = _run(tmp_path, script, pin=ZIP_SHA, sidecar=sidecar, zip_bytes=swapped)
    assert code != 0
    assert "does not match pluginSha256" in out
    assert len(calls) == 1


@needs_helm
def test_the_pin_alone_is_enough_when_the_sidecar_is_off(tmp_path, script):
    code, out, calls = _run(tmp_path, script, pin=ZIP_SHA, verify="false")
    assert code == 0, out
    assert "WARNING" not in out
    assert len(calls) == 1


@needs_helm
def test_no_pin_and_no_sidecar_still_warns(tmp_path, script):
    code, out, _ = _run(tmp_path, script, verify="false")
    assert code == 0
    assert "WARNING" in out
