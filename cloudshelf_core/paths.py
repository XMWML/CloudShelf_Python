import posixpath


def norm(path):
    cleaned = '/' + (path or '').strip().strip('/')
    return posixpath.normpath(cleaned).replace('//', '/')


def join(parent, child):
    return norm(posixpath.join(norm(parent), child))


def fmt_size(value):
    if value is None:
        return '-'
    number = float(value)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if number < 1024:
            return f'{number:.0f} {unit}' if unit == 'B' else f'{number:.1f} {unit}'
        number /= 1024
    return f'{number:.1f} PB'
