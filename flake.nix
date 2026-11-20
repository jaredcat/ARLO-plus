{
  description = "ARLO - ARC Raiders Loot Overlay (dev environment for NixOS)";

  inputs.nixpkgs.url = "nixpkgs";

  outputs =
    { self, nixpkgs }:
    let
      # PySide6 and opencv-python-headless publish Linux wheels for these.
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
    in
    {
      devShells = nixpkgs.lib.genAttrs systems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          # pyproject.toml requires >=3.11,<3.14.
          python = pkgs.python313;
          # Kernel headers and compiler for building the evdev Python package (pynput dependency).
          # mss dlopens libxcb (includes libxcb-randr, libxcb-render, libxcb-shm, libxcb-xfixes).
          # Click-through is python-xlib (pure Python). Qt's xcb plugin needs the libs in libPath.
          # glib/cairo so uv can build PyGObject; GStreamer + pipewire for Wayland capture
          # Same nixpkgs Python as glib/GStreamer. A uv-downloaded CPython is
          # patched to whatever glibc was current at install time, then breaks
          # after `nix flake update` (GLIBC_2.xx not found when importing gi).
          buildInputs = with pkgs; [
            uv
            python
            tesseract
            linuxHeaders
            gcc
            pkg-config
            libx11
            libxcb
            glib
            libffi
            cairo
            gobject-introspection
            gst_all_1.gstreamer
            gst_all_1.gst-plugins-base
            pipewire
          ];
          libPath = pkgs.lib.makeLibraryPath (with pkgs; [
            libx11
            libxcb
            glib
            cairo
            stdenv.cc.cc.lib
            # PySide6 wheel (bundled Qt) runtime deps: xcb + wayland platform plugins
            zlib
            zstd
            libxcb-util
            libGL
            libxkbcommon
            fontconfig
            freetype
            dbus
            wayland
            libdrm
            libxext
            libxrender
            libxi
            libsm
            libice
            xcb-util-cursor
            libxcb-wm
            libxcb-image
            libxcb-keysyms
            libxcb-render-util
          ]);
          giTypelibPath = pkgs.lib.makeSearchPath "lib/girepository-1.0" (with pkgs; [
            glib.out
            gobject-introspection
            gst_all_1.gstreamer
            gst_all_1.gst-plugins-base
          ]);
          gstPluginPath = pkgs.lib.makeSearchPath "lib/gstreamer-1.0" (with pkgs; [
            gst_all_1.gstreamer
            gst_all_1.gst-plugins-base
            pipewire
          ]);
        in
        {
          default = pkgs.mkShell {
            packages = buildInputs;
            C_INCLUDE_PATH = "${pkgs.linuxHeaders}/include";
            CPATH = "${pkgs.linuxHeaders}/include";
            LD_LIBRARY_PATH = libPath;
            GI_TYPELIB_PATH = giTypelibPath;
            GST_PLUGIN_SYSTEM_PATH_1_0 = gstPluginPath;
            UV_PYTHON = "${python}/bin/python3";
            UV_PYTHON_DOWNLOADS = "never";
          };
        }
      );
    };
}
