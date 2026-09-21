local LrDialogs = import 'LrDialogs'
local LrFileUtils = import 'LrFileUtils'
local LrPathUtils = import 'LrPathUtils'
local LrTasks = import 'LrTasks'
local Json = require 'Json.lua'

local Bridge = {}

function Bridge.quote(value)
    -- POSIX shell quoting: paths may contain spaces, apostrophes, $ or backticks.
    assert(not value:find('\0', 1, true), 'A path contains a null character.')
    return "'" .. value:gsub("'", "'\"'\"'") .. "'"
end

function Bridge.write(path, contents)
    local file, message = io.open(path, 'wb')
    if not file then error('Cannot write ' .. path .. ': ' .. tostring(message)) end
    local ok, writeError = file:write(contents)
    local closed, closeError = file:close()
    if not ok or not closed then error(tostring(writeError or closeError)) end
end

function Bridge.read(path)
    local file = io.open(path, 'rb')
    if not file then return nil end
    local contents = file:read('*a')
    file:close()
    return contents
end

function Bridge.readJson(path)
    local contents = Bridge.read(path)
    if not contents then return nil end
    return Json.decode(contents)
end

function Bridge.helperPath()
    if WIN_ENV then error('Camera Tools currently supports macOS only.') end
    local path = LrPathUtils.child(_PLUGIN.path, 'bin/camera-tools-helper/camera-tools-helper')
    if LrFileUtils.exists(path) ~= 'file' then
        error('The bundled importer is missing. Add the built CameraTools.lrplugin from the dist folder in Plug-in Manager. See lightroom/README.md for build instructions.')
    end
    return path
end

function Bridge.newJob()
    local root = LrPathUtils.child(LrPathUtils.getStandardFilePath('appData'), 'CameraTools/Imports')
    LrFileUtils.createAllDirectories(root)
    local path = LrFileUtils.chooseUniqueFileName(LrPathUtils.child(root, os.date('%Y%m%d-%H%M%S')))
    LrFileUtils.createAllDirectories(path)
    if LrFileUtils.exists(path) ~= 'directory' then error('Cannot create the import session folder: ' .. path) end
    return path
end

function Bridge.run(context, job, request, title)
    local helper = Bridge.helperPath()
    local responsePath = LrPathUtils.child(job, 'response.json')
    local progressPath = LrPathUtils.child(job, 'progress.json')
    local cancelPath = LrPathUtils.child(job, 'cancel')
    for _, path in ipairs { responsePath, progressPath, cancelPath } do
        if LrFileUtils.exists(path) then
            LrFileUtils.delete(path)
            if LrFileUtils.exists(path) then error('Cannot clear the previous import response: ' .. path) end
        end
    end
    request.protocol = 1
    Bridge.write(LrPathUtils.child(job, 'request.json'), Json.encode(request))
    local progress = LrDialogs.showModalProgressDialog {
        title = title, caption = 'Starting…', functionContext = context,
    }
    progress:setIndeterminate()
    local finished, exitCode, processError = false, nil, nil
    local logPath = LrPathUtils.child(job, 'helper.log')
    local command = Bridge.quote(helper) .. ' --job ' .. Bridge.quote(job)
        .. ' > ' .. Bridge.quote(logPath) .. ' 2>&1'
    LrTasks.startAsyncTask(function()
        local ok, value = LrTasks.pcall(function() return LrTasks.execute(command) end)
        if ok then exitCode = value else processError = tostring(value) end
        finished = true
    end)
    local cancelSent, cancelRequested = false, false
    local cancelProblem, displayProblem
    while not finished do
        -- Once started, keep supervising the child even if a UI update or the
        -- cancellation signal fails. Releasing the import lock early could let
        -- two importers write into the same library at once.
        local cancelOK, isCancelled = pcall(function() return progress:isCanceled() end)
        if not cancelOK then displayProblem = tostring(isCancelled) end
        if cancelOK and isCancelled then cancelRequested = true end
        if cancelRequested and not cancelSent then
            local sent, message = pcall(Bridge.write, cancelPath, 'cancel\n')
            cancelSent = sent
            cancelProblem = not sent and tostring(message) or nil
        end
        local ok, update = pcall(Bridge.readJson, progressPath)
        if ok and type(update) == 'table' and update.protocol == 1 then
            local shown, message = pcall(function()
                progress:setCaption(cancelProblem and 'Cannot send cancellation; waiting for the importer…'
                    or cancelSent and 'Stopping safely…' or update.message or '')
                if type(update.total) == 'number' and update.total > 0 then
                    progress:setPortionComplete(math.min(update.completed or 0, update.total), update.total)
                end
            end)
            if not shown then displayProblem = tostring(message) end
        end
        LrTasks.sleep(0.15)
    end
    pcall(function() progress:done() end)
    local ok, response = pcall(Bridge.readJson, responsePath)
    if not ok or type(response) ~= 'table' or response.protocol ~= 1 then
        error('The importer did not return a valid result (exit ' .. tostring(exitCode) .. '). '
            .. (processError or '') .. '\nDetails: ' .. logPath)
    end
    if response.status ~= 'ok' then error(response.error or 'The importer could not finish. See ' .. logPath) end
    local problems = request.command == 'copy' and (response.errors or {}) or (response.warnings or {})
    if cancelProblem then problems[#problems + 1] = 'Cancellation could not be sent; the importer continued until it finished: ' .. cancelProblem end
    if displayProblem then problems[#problems + 1] = 'Progress could not be displayed: ' .. displayProblem end
    if request.command == 'copy' then response.errors = problems else response.warnings = problems end
    if cancelRequested and request.command ~= 'copy' then response.cancelled = true end
    -- A cancelled or partly successful copy may have a nonzero exit code. Its
    -- completed-file manifest is still authoritative and must reach the catalog.
    return response
end

return Bridge
