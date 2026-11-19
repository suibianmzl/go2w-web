class Time:
    """lookup_transform(..., Time()) 表示取最新的变换；假 TF 只有最新值。"""

    def __init__(self, *, seconds=0, nanoseconds=0):
        self.nanoseconds = seconds * 1_000_000_000 + nanoseconds
