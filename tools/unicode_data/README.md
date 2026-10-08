# Unicode generator inputs

These gzip files preserve complete, unmodified text from Unicode's official
versioned UCD releases. They preserve upstream notices where present; the license
is also included in `src/pysual/assets/UNICODE-LICENSE.txt`.

`tools/_unicode_data.py` records the version, relative upstream path and SHA256
of every **uncompressed** input. The original URL for each is
`https://www.unicode.org/Public/<version>/ucd/<relative path>`.
The generators verify these hashes before parsing. Compression keeps roughly
4.3 MiB of source text under 750 KiB, outside the installed library and wheel.

The 17.0.0 inputs supply grapheme break, extended pictographic and Indic
conjunct-break properties. The 15.1.0 inputs supply both terminal renderers'
category and East Asian width data, plus native character-case mappings. Both versions are
intentional. Generation never uses the interpreter's `unicodedata` database or
downloads data, so supported Python versions produce the same output offline.

To update Unicode deliberately, obtain the new official versioned inputs, record
their hashes and provenance, update the explicit generator versions, and review
the resulting table and conformance-test changes. Do not replace inputs under an
existing version or change hashes merely to bypass a failed checksum check.
