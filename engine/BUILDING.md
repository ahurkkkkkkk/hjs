# Building hjs from source

The engine is one Mojo file plus one C shim. You need Linux (or WSL), gcc,
and a Mojo 1.0.0 toolchain.

## 1. Get Mojo 1.0.0

Modular stopped shipping `mojo` on conda/pixi channels; it now installs like
a Python package. Three wheels are needed:

```sh
pip download mojo==1.0.0 mojo-compiler==1.0.0 mojo-compiler-mojo-libs==1.0.0 \
    --no-deps -d /tmp/mojowheels
```

Unzip each into one tree (`mojo-compiler` provides the `bin/mojo` compiler,
`mojo-compiler-mojo-libs` provides `lib/mojo/std.mojoc`, `mojo` provides the
runtime `.so` files). Then:

```sh
chmod +x modular/bin/*
mv modular/lib/*.so  <toolchain>/lib/
mv modular/lib/mojo/std.mojoc <toolchain>/lib/mojo/
```

Create `~/.modular/modular.cfg` (the binary looks for the runtime in `/lib`
under the wrong root otherwise):

```toml
[mojo-max]
compilerrt_path = /full/path/to/<toolchain>/lib/libKGENCompilerRTShared.so
```

## 2. QuickJS

Download quickjs-2024-01-13 from bellard.org, build the static library with
position-independent code (the default build is not PIC, linking into a
`.so` will fail otherwise):

```sh
make clean
CFLAGS="-fPIC -O2" make libquickjs.a -j
```

## 3. The C shim

`hjs_shim.c` gives Mojo a small stable API over QuickJS: an engine handle,
`hjs_eval`, a cooperative pump with virtual timers, `__hjs_http` promise
ops, and helpers the host calls to resolve those promises. Build it:

```sh
gcc -O2 -fPIC -shared -I. -o libhjs.so hjs_shim.c libquickjs.a -lm -ldl -lpthread
```

## 4. The Mojo binary

```sh
MODULAR_MOJO_MAX_SYSTEM_LIBS=/usr/lib/x86_64-linux-gnu/libcurl.so.4 \
    <toolchain>/bin/mojo build -I <toolchain>/lib/mojo hjs.mojo -o hjs
```

Without the system-libs override, `mojo build` fails to link against libcurl
(`DSO missing from command line`) even though `mojo run` works. At run time
export `LD_LIBRARY_PATH` to include `<toolchain>/lib` and the directory
holding `libhjs.so`.

## Gotchas learned the hard way

* `JS_NewPromiseCapability` wants `JSValue resolving_funcs[2]`. Handing it
  one pointer smashes the stack when the page calls `fetch()`.
* QuickJS 2024 has no `JS_PendingJob` / `JS_HasException`; loop
  `JS_ExecutePendingJob` and check the return value.
* Mojo has `#` comments only, no `//`. `fn` is gone (use `def`). Tuple
  returns in the compiler build used here reject `-> (Int, String)`; use a
  `@fieldwise_init` struct.
* Never index a Mojo `String` by raw byte offset (`s[byte=i]` aborts on
  mid-codepoint offsets). Scan `s.as_bytes()` and copy whole codepoints
  using the UTF-8 lead byte.
* `String.lower()` can change byte offsets (İ expands). Never align a
  lowercased copy against original offsets; compare case-insensitively on
  small ASCII windows instead.
* Declaring your own `external_call["fclose", ...]` conflicts with the
  stdlib's declaration if anything in the module graph also declares it.
  The profile reader sidesteps this with `popen("cat ...")`.
* `as_c_string_slice()` is mutating and consumes its receiver; copy the
  string to a local first and take `byte_length()` before converting.
* Passing Mojo values into C as `void*`: convert the pointer to an address
  with `Int(ptr)` and take an `Int`/`c_size_t` on the wrapper side.
* `CURLOPT_COOKIEFILE` is 10000 + 31 = 10031. Getting that wrong makes
  cookies silently do nothing.
