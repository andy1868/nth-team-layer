"""5 层上下文压缩管线 — 从廉价到昂贵"""

from .pipeline import CompressionPipeline, CompressionStage

__all__ = ["CompressionPipeline", "CompressionStage"]
