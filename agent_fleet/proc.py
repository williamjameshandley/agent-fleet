from pathlib import Path


def start_time(pid, stat=None):
    stat = Path(f"/proc/{pid}/stat").read_text() if stat is None else stat
    _, separator, fields = stat.rpartition(")")
    values = fields.split()
    if not separator or len(values) < 20:
        raise ValueError(f"invalid stat record for process {pid}")
    return int(values[19])
