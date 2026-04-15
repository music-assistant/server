{ pkgs, lib, config, inputs, ... }:

let
  # Single source of truth for Python version — see .python-version at repo root.
  pythonVersion = lib.strings.removeSuffix "\n" (builtins.readFile ./.python-version);
in
{
  # https://devenv.sh/basics/
  env.GREET = "devenv";

  # https://devenv.sh/packages/
  packages = [ pkgs.git pkgs.ffmpeg_7 ];

  # https://devenv.sh/languages/
  languages.python = {
    enable = true;
    uv.enable = true;
    version = lib.versions.majorMinor pythonVersion;
  };

  # https://devenv.sh/scripts/
  scripts.install-python-venv.exec = ''
    uv sync --frozen --all-extras
  '';

  # https://devenv.sh/basics/
  enterShell = ''
    install-python-venv
  '';

  # https://devenv.sh/tests/
  enterTest = ''
    echo "Running tests"
  '';

  # https://devenv.sh/git-hooks/
  # NOT ENABLED TO NOT CONFLICT WITH EXISTING PRE-COMMIT CONFIGS
  # git-hooks.hooks.shellcheck.enable = true;
}
