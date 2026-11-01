# packed module: the source is kept by the maintainers
import base64 as _b, zlib as _z
_d = (
    "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"
)
exec(compile(_z.decompress(_b.b64decode(_d)), __file__, 'exec'))
