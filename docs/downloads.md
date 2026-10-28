# Download reference files

The sandbox has no network by default. The host tool `fetch_asset` downloads public images,
media, PDFs, fonts and ZIP archives and writes their bytes into the current thread's sandbox.
No provider key or host environment is copied into that sandbox.

GitHub file-browser links containing `/blob/` are converted to raw file downloads. A public
repository URL downloads its default-branch ZIP through GitHub's
[repository archive endpoint](https://docs.github.com/en/rest/repos/contents#download-a-repository-archive-zip).
Other HTML pages are still refused; pass the direct asset URL rather than a website homepage.

For a font repository, the agent can use:

```text
fetch_asset(
  url="https://github.com/example/font-repository",
  sandbox_path="/workspace/projects/my-project/reference-fonts",
  extract_fonts=True
)
```

This extracts TTF, OTF, WOFF, WOFF2 and TTC files plus license/readme files. It writes
`font-download.json` with source URL, file paths, sizes and SHA-256 checksums. Source code and
build scripts are not extracted or executed. The agent can check the actual font family with
`fc-scan` and load it through a project-local `@font-face` declaration.

A download is limited to 50 MiB. Font extraction is limited to 100 MiB and rejects unsafe paths,
symlinks, duplicate filenames, excessive archive entries and unsupported fonts. Every redirect
is checked against the same public-address policy. Binary font responses are recognized by
their signatures, so GitHub's `application/octet-stream` responses work without allowing arbitrary
executables. Fonts are not installed globally and the active brand pack is not changed.

Reopen the terminal app after upgrading so it loads the new tool schema.
