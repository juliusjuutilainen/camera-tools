local LrApplication = import 'LrApplication'
local LrBinding = import 'LrBinding'
local LrDialogs = import 'LrDialogs'
local LrFileUtils = import 'LrFileUtils'
local LrFunctionContext = import 'LrFunctionContext'
local LrPathUtils = import 'LrPathUtils'
local LrPrefs = import 'LrPrefs'
local LrShell = import 'LrShell'
local LrTasks = import 'LrTasks'
local LrView = import 'LrView'
local Bridge = require 'Bridge.lua'
local Catalog = require 'Catalog.lua'
local Json = require 'Json.lua'

local App = {}
local running = false
local bind = LrView.bind

local function trim(value)
    return (value or ''):match('^%s*(.-)%s*$')
end

local function chooseFolder(props, key, title)
    local selection = LrDialogs.runOpenPanel {
        title = title, prompt = 'Choose', canChooseFiles = false,
        canChooseDirectories = true, canCreateDirectories = key == 'destination',
        allowsMultipleSelection = false,
        initialDirectory = props[key] ~= '' and props[key] or LrPathUtils.getStandardFilePath('home'),
    }
    if selection then props[key] = selection[1] end
end

local function settingsDialog(props, sources)
    local f = LrView.osFactory()
    local sourcePaths = {}
    for _, source in ipairs(sources) do sourcePaths[#sourcePaths + 1] = source.path end
    local hint = #sources == 0 and 'No mounted camera found. Choose a folder, or connect a card and refresh.'
        or (#sources == 1 and 'One camera card detected.' or 'Choose a camera card from the source menu.')
    return LrDialogs.presentModalDialog {
        title = 'Import from camera', actionVerb = 'Preview import', otherVerb = 'Refresh cards',
        contents = f:column {
            bind_to_object = props, spacing = f:dialog_spacing(),
            f:static_text { title = 'Copy into dated folders and add to the current Lightroom catalog.' },
            f:static_text { title = 'Source', font = '<system/bold>' },
            f:row {
                spacing = f:control_spacing(),
                f:combo_box { value = bind 'source', items = sourcePaths, width_in_chars = 55, immediate = true },
                f:push_button { title = 'Choose folder…', action = function() chooseFolder(props, 'source', 'Choose camera or source folder') end },
            },
            f:static_text { title = hint, width_in_chars = 80, height_in_lines = 2 },
            f:static_text { title = 'Destination', font = '<system/bold>' },
            f:row {
                spacing = f:control_spacing(),
                f:edit_field { value = bind 'destination', width_in_chars = 55, immediate = true },
                f:push_button { title = 'Choose destination…', action = function() chooseFolder(props, 'destination', 'Choose photo library') end },
            },
            f:static_text { title = 'Files will be organized into YYYY/MM/DD folders. Originals stay on the card.' },
            f:separator { fill_horizontal = 1 },
            f:row {
                spacing = f:control_spacing(),
                f:checkbox { title = 'Only on or after', value = bind 'useCutoff' },
                f:edit_field { value = bind 'cutoff', enabled = bind 'useCutoff', width_in_chars = 12, immediate = true },
                f:static_text { title = 'YYYY-MM-DD (inclusive)' },
            },
            f:row {
                spacing = f:control_spacing(),
                f:static_text { title = 'Date used for filtering and folders:' },
                f:popup_menu {
                    value = bind 'dateBasis',
                    items = {
                        { title = 'Date taken (EXIF)', value = 'capture' },
                        { title = 'File modified date', value = 'modified' },
                    },
                },
            },
            f:checkbox { title = 'Include videos', value = bind 'includeVideos' },
        },
    }
end

local function pagedText(context, text)
    local f = LrView.osFactory()
    local props = LrBinding.makePropertyTable(context)
    local lines = {}
    for line in ((text or '') .. '\n'):gmatch('(.-)\n') do lines[#lines + 1] = line end
    local page, size = 1, 80
    local pages = math.max(1, math.ceil(#lines / size))
    local function showPage()
        local visible = {}
        for index = (page - 1) * size + 1, math.min(page * size, #lines) do visible[#visible + 1] = lines[index] end
        props.text = table.concat(visible, '\n')
        props.pageLabel = string.format('Page %d of %d', page, pages)
        props.previous = page > 1
        props.next = page < pages
    end
    showPage()
    return f:column {
        bind_to_object = props, spacing = f:control_spacing(),
        f:scrolled_view {
            width = 760, height = 300, horizontal_scroller = true, vertical_scroller = true,
            f:static_text { title = bind 'text', height_in_lines = math.min(size, #lines), selectable = true },
        },
        f:row {
            spacing = f:control_spacing(),
            f:push_button { title = 'Previous', enabled = bind 'previous', action = function() page = page - 1; showPage() end },
            f:static_text { title = bind 'pageLabel', width_in_chars = 22, alignment = 'center' },
            f:push_button { title = 'Next', enabled = bind 'next', action = function() page = page + 1; showPage() end },
        },
    }
end

local function previewDialog(context, preview, catalog)
    local f = LrView.osFactory()
    local notes = string.format('%d files • %d already present • %d names in use • %.1f MiB to copy • %d excluded by filters',
        preview.count, preview.present or 0, preview.taken or 0,
        (preview.bytes_to_copy or preview.total_bytes) / (1024 * 1024), preview.filtered)
    if preview.fallback_count > 0 then
        notes = notes .. string.format('\n%d files use modification dates because capture dates were unavailable.', preview.fallback_count)
    end
    local details = preview.preview_text or ''
    if #(preview.warnings or {}) > 0 then
        details = 'Scan warnings\n' .. table.concat(preview.warnings, '\n') .. '\n\n' .. details
    end
    return LrDialogs.presentModalDialog {
        title = 'Review camera import', actionVerb = 'Import files', otherVerb = 'Change options',
        contents = f:column {
            spacing = f:dialog_spacing(),
            f:static_text { title = notes, width_in_chars = 90, height_in_lines = 2 },
            f:static_text { title = 'Destination: ' .. preview.destination, width_in_chars = 90, height_in_lines = 2, selectable = true },
            f:static_text { title = 'Catalog: ' .. catalog:getPath(), width_in_chars = 90, height_in_lines = 2 },
            pagedText(context, details),
            f:static_text {
                title = 'Each file is judged on its own: a file already present in its date folder is verified and never copied again; a different file with the same name gets a suffix.\nCancelling the copy keeps completed files and adds those files to this catalog.',
                width_in_chars = 90, height_in_lines = 3,
            },
        },
    }
end

local function showResult(context, job, copy, added)
    local f = LrView.osFactory()
    local problems = {}
    for _, message in ipairs(copy.errors or {}) do problems[#problems + 1] = 'Copy: ' .. message end
    for _, message in ipairs(added.errors) do problems[#problems + 1] = 'Catalog: ' .. message end
    local incomplete = copy.cancelled or added.cancelled or added.pending > 0 or #problems > 0
    local summary = string.format('%d files copied • %d already on disk • %d copied with new names\n%d added to Lightroom • %d already in this catalog',
        copy.copied, copy.skipped, copy.renamed, added.added, added.existing)
    if copy.cancelled then summary = summary .. '\nCopying was cancelled; completed files were kept.' end
    if added.pending > 0 then summary = summary .. string.format('\n%d media files remain to be checked for catalog import.', added.pending) end
    local details = #problems > 0 and table.concat(problems, '\n') or 'No errors reported.'
    Bridge.write(LrPathUtils.child(job, 'catalog-result.json'), Json.encode(added))
    local choice = LrDialogs.presentModalDialog {
        title = incomplete and 'Import finished with items to review' or 'Import complete',
        actionVerb = 'Done', cancelVerb = 'Close', otherVerb = 'Open import report',
        contents = f:column {
            spacing = f:dialog_spacing(),
            f:static_text { title = summary, width_in_chars = 90, height_in_lines = 5 },
            pagedText(context, details),
            f:static_text {
                title = 'If catalog import was interrupted, use File → Plug-in Extras → Retry last camera catalog import…\nFor files that were not copied, start a new import from the card.',
                width_in_chars = 90, height_in_lines = 3,
            },
        },
    }
    if choice == 'other' then LrShell.revealInShell(job) end
end

function App.retry(context, prefs)
    local job = prefs.lastJob
    if not job then LrDialogs.message('No previous camera import', 'Import files from a camera first.'); return end
    local savedCatalog = Bridge.readJson(LrPathUtils.child(job, 'catalog.json'))
    local copy = Bridge.readJson(LrPathUtils.child(job, 'response.json'))
    if not savedCatalog or not copy or copy.protocol ~= 1 or copy.status ~= 'ok' or type(copy.files) ~= 'table' then
        error('The previous copy did not leave a completed import report. Start a new import from the same card; files already copied will be checked and reused.')
    end
    local catalog = LrApplication.activeCatalog()
    if savedCatalog.path ~= catalog:getPath() then
        error('Open the catalog used for this import, then retry:\n' .. tostring(savedCatalog.path))
    end
    showResult(context, job, copy, Catalog.add(context, catalog, copy.files))
end

function App.import(context, prefs)
    Bridge.helperPath()
    local catalog = LrApplication.activeCatalog()
    local props = LrBinding.makePropertyTable(context)
    props.source = ''
    props.destination = prefs.destination or LrPathUtils.getStandardFilePath('pictures')
    props.useCutoff = prefs.useCutoff == true
    props.cutoff = prefs.cutoff or os.date('%Y-%m-%d')
    props.dateBasis = prefs.dateBasis or 'capture'
    props.includeVideos = prefs.includeVideos ~= false
    local job = Bridge.newJob()
    local keepJob = false
    context:addCleanupHandler(function()
        if not keepJob then LrFileUtils.delete(job) end
    end)
    local discovery = Bridge.run(context, job, { command = 'discover' }, 'Detecting camera cards')
    if discovery.cancelled then return end
    if #discovery.sources == 1 then props.source = discovery.sources[1].path end
    while true do
        local choice = settingsDialog(props, discovery.sources)
        if choice == 'cancel' then return end
        if choice == 'other' then
            discovery = Bridge.run(context, job, { command = 'discover' }, 'Detecting camera cards')
            if discovery.cancelled then return end
            props.source = #discovery.sources == 1 and discovery.sources[1].path or ''
        elseif trim(props.source) == '' or trim(props.destination) == '' then
            LrDialogs.message('Choose both folders', 'Select a source and a destination before previewing.', 'warning')
        elseif props.useCutoff and not trim(props.cutoff):match('^%d%d%d%d%-%d%d%-%d%d$') then
            LrDialogs.message('Enter a date', 'Use YYYY-MM-DD, for example 2026-09-01.', 'warning')
        else
            local ok, preview = LrTasks.pcall(function()
                return Bridge.run(context, job, {
                    command = 'preview', source = trim(props.source), destination = trim(props.destination),
                    cutoff = props.useCutoff and trim(props.cutoff) or '',
                    date_basis = props.dateBasis, include_videos = props.includeVideos,
                }, 'Previewing camera import')
            end)
            if not ok then
                LrDialogs.message('Could not preview import', tostring(preview), 'warning')
            elseif preview.cancelled then
                return
            else
                prefs.destination, prefs.useCutoff, prefs.cutoff = props.destination, props.useCutoff, props.cutoff
                prefs.dateBasis, prefs.includeVideos = props.dateBasis, props.includeVideos
                if preview.count == 0 then
                    LrDialogs.message('No matching files', table.concat(preview.warnings or {}, '\n') .. '\nTry a different source or an earlier date.')
                else
                    local review = previewDialog(context, preview, catalog)
                    if review == 'cancel' then return end
                    if review == 'ok' then
                        if LrApplication.activeCatalog():getPath() ~= catalog:getPath() then error('The active catalog changed. Start the import again.') end
                        Bridge.write(LrPathUtils.child(job, 'catalog.json'), Json.encode { path = catalog:getPath() })
                        keepJob = true
                        prefs.lastJob = job
                        local copied = Bridge.run(context, job, { command = 'copy' }, 'Copying camera files')
                        showResult(context, job, copied, Catalog.add(context, catalog, copied.files))
                        return
                    end
                end
            end
        end
    end
end

function App.start(retry)
    if running then LrDialogs.message('Camera import is already running', 'Finish or cancel the current import first.'); return end
    running = true
    LrTasks.startAsyncTask(function()
        LrFunctionContext.callWithContext('Camera Tools', function(context)
            context:addCleanupHandler(function() running = false end)
            LrDialogs.attachErrorDialogToFunctionContext(context)
            local prefs = LrPrefs.prefsForPlugin()
            if retry then App.retry(context, prefs) else App.import(context, prefs) end
        end)
    end)
end

return App
