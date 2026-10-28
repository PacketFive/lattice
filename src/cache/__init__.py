# SPDX-License-Identifier: GPL-2.0
"""Binary caches: signed archives of store paths (RFC 0001 section 14).

archive.py makes and unpacks deterministic archives, signing.py holds the
Ed25519 keys, metadata.py the files a cache serves, provenance.py the
signed build records, http.py and cvmfs.py the two kinds of cache,
substitute.py fetches paths from caches and push.py uploads them.
"""
