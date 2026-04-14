{ pkgs, lib, config, inputs, ... }:

{
  # https://devenv.sh/basics/
  env.GREET = "devenv";

  # https://devenv.sh/packages/
  packages = [ pkgs.git pkgs.ffmpeg_7 ];

  # https://devenv.sh/languages/
  languages.python = {
    enable = true;
    uv.enable = true;
    version = "3.13";
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
