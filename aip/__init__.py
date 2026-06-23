"""AI Package (.aip) compression toolkit."""

from .codec import AIPError, CompressionResult, compress, decompress

__all__ = ["AIPError", "CompressionResult", "compress", "decompress"]
__version__ = "0.1.0"

