{
  description = "Music Assistant development shell";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { nixpkgs, ... }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];

      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      devShells = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
          python = pkgs.python314;

          commonPackages = with pkgs; [
            ffmpeg
            git
            pkg-config
            python
            uv
          ];

          buildPackages = with pkgs; [
            stdenv.cc
          ];

          nativeLibraries = with pkgs; [
            bzip2
            chromaprint
            curl
            expat
            ffmpeg
            libffi
            libsndfile
            openssl
            portaudio
            sqlite
            stdenv.cc.cc.lib
            zlib
            zstd
          ] ++ lib.optionals stdenv.isLinux [
            alsa-lib
            avahi
            json_c
            libconfuse
            libgcrypt
            libplist
            libpulseaudio
            libsodium
            libunistring
            libuuid
            nfs-utils
            util-linux
          ];
        in
        {
          default = pkgs.mkShell {
            packages = commonPackages ++ buildPackages ++ nativeLibraries;

            env = {
              UV_PROJECT_ENVIRONMENT = ".venv";
              LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath nativeLibraries;
            };

            shellHook = ''
              echo "Music Assistant dev shell"
              echo "Python: $(${python}/bin/python --version)"
              if [ -f .venv/bin/activate ]; then
                source .venv/bin/activate
                echo "Activated .venv"
              else
                echo "Run scripts/setup.sh once, then pytest to run tests."
              fi
            '';
          };
        });
    };
}
