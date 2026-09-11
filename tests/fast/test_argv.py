"""Fast-layer test: static ``docker run`` argv construction.

No Docker required — this asserts the argv the caller builds contains
every required containment flag, exactly two mounts and nothing else, and
never references ``/var/run/docker.sock`` or ``--privileged`` or ``--rm``.
"""

from __future__ import annotations

import d33d.render_worker as rw


def _argv() -> list[str]:
    name = "render-0123abcd"
    return rw.build_docker_argv("openscad-test:latest", name)


def test_contains_every_required_containment_flag() -> None:
    argv = _argv()
    for flag in (
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--read-only",
        "--tmpfs",
        "/tmp",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        "--cpus",
        "--pids-limit",
    ):
        assert flag in argv, f"missing {flag!r} in argv"


def test_contains_exactly_two_mounts_and_nothing_else() -> None:
    argv = _argv()
    # The rw /work named volume
    assert "--volume" in argv
    idx = argv.index("--volume")
    assert argv[idx + 1].endswith(":/work")
    # The /tmp tmpfs
    assert "--tmpfs" in argv
    tmpfs_idx = argv.index("--tmpfs")
    assert argv[tmpfs_idx + 1] == "/tmp"
    # No other mount-type flags
    assert argv.count("--volume") == 1
    assert argv.count("--tmpfs") == 1
    # No other mount mechanism
    for other in ("--mount", "-v", "--bind"):
        assert other not in argv, f"unexpected mount flag {other!r}"


def test_no_docker_sock_reference() -> None:
    argv = _argv()
    flat = " ".join(argv)
    assert "/var/run/docker.sock" not in flat
    assert "docker.sock" not in flat


def test_never_contains_privileged_or_rm() -> None:
    argv = _argv()
    assert "--privileged" not in argv
    assert "--rm" not in argv


def test_container_name_is_the_render_name() -> None:
    argv = _argv()
    assert "--name" in argv
    idx = argv.index("--name")
    assert argv[idx + 1] == "render-0123abcd"


def test_memory_cpu_pids_limits_are_present_with_defaults() -> None:
    argv = _argv()
    mem_idx = argv.index("--memory")
    assert argv[mem_idx + 1] == rw.DEFAULT_MEMORY_LIMIT
    cpus_idx = argv.index("--cpus")
    assert argv[cpus_idx + 1] == rw.DEFAULT_CPU_LIMIT
    pids_idx = argv.index("--pids-limit")
    assert argv[pids_idx + 1] == str(rw.DEFAULT_PID_LIMIT)


def test_defines_are_passed_as_dash_d_pairs() -> None:
    name = "render-0123abcd"
    params = rw.RenderParams(defines={"W": "20", "H": "10"})
    argv = rw.build_docker_argv("img", name, params=params)
    assert "-D" in argv
    d_idx = argv.index("-D")
    assert argv[d_idx + 1] == "W=20"
    # second -D
    assert argv.count("-D") == 2
    d_idx2 = argv.index("-D", d_idx + 2)
    assert argv[d_idx2 + 1] == "H=10"


def test_empty_defines_add_no_dash_d_flags() -> None:
    argv = _argv()
    assert "-D" not in argv


def test_volume_matches_name_by_default() -> None:
    name = "render-0123abcd"
    argv = rw.build_docker_argv("img", name)
    vol_idx = argv.index("--volume")
    assert argv[vol_idx + 1] == f"{name}:/work"


def test_explicit_volume_overrides_the_default() -> None:
    argv = rw.build_docker_argv(
        "img", "render-0123abcd", workdir_volume="render-ffffeeee"
    )
    vol_idx = argv.index("--volume")
    assert argv[vol_idx + 1] == "render-ffffeeee:/work"


def test_dict_params_are_accepted_and_parsed() -> None:
    argv = rw.build_docker_argv(
        "img", "render-0123abcd", params={"defines": {"W": "5"}}
    )
    assert "-D" in argv
    assert "W=5" in argv


def test_name_pattern_matches_render_dash_8_hex() -> None:
    assert rw.validate_render_name("render-0123abcd")
    assert not rw.validate_render_name("render-xyz")
    assert not rw.validate_render_name("render-0123abc")  # too short
    assert not rw.validate_render_name("render-0123abcde")  # too long
    assert not rw.validate_render_name("0123abcd")


def test_new_render_name_matches_pattern() -> None:
    name = rw.new_render_name()
    assert rw.validate_render_name(name)


def test_two_names_are_distinct() -> None:
    # Parallel renders must use distinct container AND volume names.
    a = rw.new_render_name()
    b = rw.new_render_name()
    assert a != b
