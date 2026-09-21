return {
    LrSdkVersion = 6.0,
    LrSdkMinimumVersion = 6.0,
    LrToolkitIdentifier = 'fi.julius.camera-tools',
    LrPluginName = 'Camera Tools',
    LrPluginInfoProvider = 'PluginInfo.lua',
    VERSION = { major = 0, minor = 3, revision = 0, build = 1 },
    LrExportMenuItems = {
        { title = 'Import from camera…', file = 'Import.lua' },
        { title = 'Retry last camera catalog import…', file = 'Retry.lua' },
    },
}
