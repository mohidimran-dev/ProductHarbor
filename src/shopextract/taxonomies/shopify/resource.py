"""Standalone stdlib-only prepared-resource verification, shared by loader/build hook."""
import gzip
import hashlib
import json
import os
import re
from pathlib import Path

COMPRESSED_LIMIT = 8*1024*1024
DECODED_LIMIT = 128*1024*1024


class ResourceIntegrityError(ValueError):
    pass


COUNT_FIELDS={'categories','base_attributes','extensions','values','context_links','parent_links','ancestor_links','return_reason_links'}
IDENTITY_FIELDS={'schema_version','namespace','release','commit','fingerprint','source_hashes','counts'}


def safe_release(value):
    if not isinstance(value,str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}',value) is None:
        raise ResourceIntegrityError('Unsafe catalog release component')


def hex_digest(value, *, length=64):
    if not isinstance(value,str) or re.fullmatch('[0-9a-f]{'+str(length)+'}',value) is None:
        raise ResourceIntegrityError('Invalid catalog hexadecimal identity')


def validate_source_hashes(value):
    if not isinstance(value,(list,tuple)) or len(value)!=3:
        raise ResourceIntegrityError('Invalid source hash references')
    names=[]
    for pair in value:
        if not isinstance(pair,(list,tuple)) or len(pair)!=2 or not isinstance(pair[0],str):
            raise ResourceIntegrityError('Invalid source hash pair')
        names.append(pair[0]);hex_digest(pair[1])
    if set(names)!={'taxonomy.en.json.gz','attributes.yml','LICENSE'} or len(set(names))!=3:
        raise ResourceIntegrityError('Incomplete source hash identities')


def validate_manifest_identity(manifest):
    if not isinstance(manifest,dict) or set(manifest)!=IDENTITY_FIELDS:
        raise ResourceIntegrityError('Invalid staged manifest identity fields')
    if type(manifest['schema_version']) is not int or manifest['schema_version']!=1:
        raise ResourceIntegrityError('Invalid manifest version type')
    if not isinstance(manifest['namespace'],str) or not manifest['namespace']:
        raise ResourceIntegrityError('Invalid manifest namespace')
    safe_release(manifest['release']);hex_digest(manifest['fingerprint']);hex_digest(manifest['commit'],length=40)
    validate_source_hashes(manifest['source_hashes'])
    counts=manifest['counts']
    if not isinstance(counts,dict) or set(counts)!=COUNT_FIELDS or any(type(v) is not int or v<0 for v in counts.values()):
        raise ResourceIntegrityError('Invalid catalog counts')


def read_gzip(path, *, compressed_limit=COMPRESSED_LIMIT, decoded_limit=DECODED_LIMIT):
    path=Path(path)
    if path.stat().st_size > compressed_limit:
        raise ResourceIntegrityError('Compressed catalog byte cap exceeded')
    with gzip.open(path,'rb') as handle:
        data=handle.read(decoded_limit+1)
    if len(data)>decoded_limit:
        raise ResourceIntegrityError('Decoded catalog byte cap exceeded')
    return data


def write_reproducible_gzip(path, payload: bytes) -> str:
    """Write gzip with fixed header fields; return SHA-256 of compressed bytes.

    mtime=0 and empty filename avoid wall-clock/path nondeterminism. The DEFLATE
    body still depends on the process zlib implementation (classic zlib vs
    zlib-ng), so callers must treat pin.resource_sha256 as a reference bitstream
    and rely on fingerprint checks for logical identity across platforms.
    """
    path=Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('wb') as output:
        with gzip.GzipFile(fileobj=output, mode='wb', mtime=0, filename='', compresslevel=9) as compressor:
            # Force OS=255 (unknown) when the interpreter exposes the attribute.
            if hasattr(compressor, 'os'):
                compressor.os = 255
            compressor.write(payload)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _decoded_fingerprint(catalog_path: Path) -> str:
    """SHA-256 of decoded catalog bytes (matches identity.fingerprint for canonical JSON)."""
    return hashlib.sha256(read_gzip(catalog_path)).hexdigest()


def verify_resource_files(directory, *, expected=None):
    """Verify prepared catalog bytes.

    Always:
      - manifest schema/identity
      - LICENSE and catalog.json.gz hashes match the manifest (self-consistency)
      - decoded catalog fingerprint equals manifest fingerprint

    When ``expected`` (pin.json) is supplied:
      - all identity fields and license_sha256 must match the pin
      - decoded catalog fingerprint must match pin.fingerprint (logical authority)
      - pin.resource_sha256 is the reference compressor bitstream (Linux/classic zlib
        from the original release). Equality is required only when
        PRODUCTHARBOR_STRICT_RESOURCE_SHA256=1, or when the local bitstream already
        matches (no trust of divergent zlib-ng bytes as a new pin).
    """
    directory=Path(directory)
    manifest_path=directory/'manifest.json'
    if manifest_path.stat().st_size>65536:
        raise ResourceIntegrityError('Manifest byte cap exceeded')
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    required={'schema_version','namespace','release','commit','fingerprint','source_hashes','counts','resource_sha256','license_sha256'}
    if not isinstance(manifest,dict) or set(manifest)!=required:
        raise ResourceIntegrityError('Invalid catalog manifest schema')
    validate_manifest_identity({key:manifest[key] for key in IDENTITY_FIELDS})
    hex_digest(manifest['resource_sha256']);hex_digest(manifest['license_sha256'])

    catalog_path=directory/'catalog.json.gz'
    license_path=directory/'LICENSE'
    for name,key,limit,path in [
        ('catalog.json.gz','resource_sha256',COMPRESSED_LIMIT,catalog_path),
        ('LICENSE','license_sha256',8192,license_path),
    ]:
        if path.stat().st_size>limit or hashlib.sha256(path.read_bytes()).hexdigest()!=manifest[key]:
            raise ResourceIntegrityError('Catalog resource hash/size mismatch: '+name)

    decoded_fingerprint=_decoded_fingerprint(catalog_path)
    if decoded_fingerprint!=manifest['fingerprint']:
        raise ResourceIntegrityError('Catalog decoded fingerprint differs from manifest fingerprint')

    if expected is not None:
        if not isinstance(expected, dict):
            raise ResourceIntegrityError('Invalid pin expectation')
        for key,value in expected.items():
            if key=='resource_sha256':
                continue
            if manifest.get(key)!=value:
                raise ResourceIntegrityError('Catalog differs from pinned '+key)
        if 'fingerprint' in expected and decoded_fingerprint!=expected.get('fingerprint'):
            raise ResourceIntegrityError('Catalog differs from pinned fingerprint')
        # Full pin.json includes resource_sha256 (reference classic-zlib bitstream).
        # Staged identity expectations used during publish omit it.
        if 'resource_sha256' in expected:
            expected_resource=expected.get('resource_sha256')
            hex_digest(expected_resource)
            actual_resource=manifest['resource_sha256']
            strict=os.environ.get('PRODUCTHARBOR_STRICT_RESOURCE_SHA256','').strip()=='1'
            if actual_resource!=expected_resource and strict:
                raise ResourceIntegrityError(
                    'Catalog compressed bitstream differs from pinned resource_sha256 '
                    '(strict mode; zlib implementations may differ across platforms)'
                )
            # Non-strict: divergent compressor (e.g. Windows zlib-ng vs Linux classic
            # zlib) is accepted only after decoded fingerprint already matched above.
            # Never rewrite pin.json to the local hash automatically.
    return manifest
