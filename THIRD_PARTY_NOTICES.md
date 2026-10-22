# Third-party resources

Our original Python code is MIT licensed. The ASN.1 data resource is separately
owned material and is not relicensed under MIT.

## ITU-T H.245 ASN.1 definitions

`src/h324/data/MULTIMEDIA-SYSTEM-CONTROL.asn` contains the H.245 v15 (2009)
MultimediaSystemControl ASN.1 definitions, taken from ITU-T as noted in its
preserved header. Retrieved from the Wireshark source tree at revision
`770a86059d70d8077f42defa434f89cbc55c7c14`:
https://gitlab.com/wireshark/wireshark/-/blob/770a86059d70d8077f42defa434f89cbc55c7c14/epan/dissectors/asn1/h245/MULTIMEDIA-SYSTEM-CONTROL.asn

SHA-256: `18fa2c3752a9868f716136d25e106f39cf392c40ebc378ecfbc1deadbc8ac747`.

Redistribution/use basis: ITU-T Guidelines on Software Copyright, section
2.2.2, treats ASN.1 data used by implementers as free from ITU copyright
assertions without a specific licence:
https://www.itu.int/dms_pub/itu-t/oth/04/04/T04040000040004PDFE.pdf

The file's original ITU attribution is preserved. No Wireshark implementation
code is included. The schema version does not imply implementation of all its
alternatives: this package implements a finite baseline profile with the
H.245 version-1 protocol identifier. ITU Recommendation PDFs are not included.

## Dependencies

asn1tools (MIT), pyparsing (MIT), bitstruct (MIT), and pyserial (BSD-3-Clause)
are installed as dependencies under their own licences. They are not
relicensed by this project. PyPI metadata records the applicable dependencies.

## Synthetic media tests

The small audio and video elementary streams in tests/fixtures/media were
generated from an original synthetic tone and moving square, not a recording
of a person or a captured telephone call. Their manifest contains no lab paths
or command history. These original test assets are covered by our MIT licence.
