DEFAULT_SETTINGS = {
    'automatic_sync': True,
    'max_workers': 3,
    'language': 'system',
    'preview_enabled': True,
    'preview_extensions': 'txt,md,json,csv,log,py,xml,yaml,yml,ini,html,css,js',
    'preview_max_bytes': 1024 * 1024,
    'download_directory': '',
    'ask_download_destination': True,
    'automatic_connect_profile_id': '',
}


def normalized_settings(value):
    settings = dict(DEFAULT_SETTINGS)
    if isinstance(value, dict):
        settings.update(value)
    settings['max_workers'] = max(1, min(8, int(settings['max_workers'])))
    settings['preview_max_bytes'] = max(1, int(settings['preview_max_bytes']))
    settings['language'] = settings['language'] if settings['language'] in ('system', 'zh', 'en') else 'system'
    settings['automatic_connect_profile_id'] = str(settings.get('automatic_connect_profile_id') or '')
    for key in ('preview_enabled', 'ask_download_destination', 'automatic_sync'):
        settings[key] = bool(settings[key])
    return settings
