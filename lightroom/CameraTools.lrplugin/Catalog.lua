local LrApplication = import 'LrApplication'
local LrDialogs = import 'LrDialogs'
local LrFileUtils = import 'LrFileUtils'
local LrPathUtils = import 'LrPathUtils'
local LrTasks = import 'LrTasks'

local Catalog = {}

function Catalog.add(context, catalog, files)
    local result = { added = 0, existing = 0, errors = {}, cancelled = false, pending = 0 }
    local progress = LrDialogs.showModalProgressDialog {
        title = 'Adding photos to Lightroom', caption = 'Opening catalog…', functionContext = context,
    }
    local seen, completed = {}, 0
    for first = 1, #files, 25 do
        if progress:isCanceled() then result.cancelled = true; break end
        if LrApplication.activeCatalog():getPath() ~= catalog:getPath() then
            result.errors[#result.errors + 1] = 'The active catalog changed. Reopen the original catalog and retry.'
            break
        end
        local entered = false
        -- A write gate is a transaction. Publish counts only after it commits.
        local batch = { added = 0, existing = 0, completed = 0, errors = {}, seen = {} }
        local ok, failure = LrTasks.pcall(function()
            catalog:withWriteAccessDo('Import from camera', function()
                entered = true
                for index = first, math.min(first + 24, #files) do
                    if progress:isCanceled() then result.cancelled = true; break end
                    local item = files[index]
                    local path = item.path
                    progress:setCaption(LrPathUtils.leafName(path or 'Unknown file'))
                    local success, message = LrTasks.pcall(function()
                        if item.kind ~= 'raw' and item.kind ~= 'photo' and item.kind ~= 'video' then
                            error('Not an importable media file.')
                        end
                        if type(path) ~= 'string' or not LrPathUtils.isAbsolute(path) then
                            error('The destination path is invalid.')
                        end
                        if not seen[path] and not batch.seen[path] then
                            if LrFileUtils.exists(path) ~= 'file' then error('The copied file is no longer available.') end
                            if catalog:findPhotoByPath(path) then
                                batch.existing = batch.existing + 1
                            else
                                local photo = catalog:addPhoto(path)
                                if not photo then error('Lightroom did not accept this file format.') end
                                batch.added = batch.added + 1
                            end
                            batch.seen[path] = true
                        end
                    end)
                    if not success then batch.errors[#batch.errors + 1] = tostring(path) .. ': ' .. tostring(message) end
                    batch.completed = batch.completed + 1
                    progress:setPortionComplete(completed + batch.completed, #files)
                    LrTasks.yield()
                end
            end, { timeout = 30 })
            if not entered then error('The catalog is busy. Retry the catalog import when Lightroom finishes its current task.') end
        end)
        if not ok then result.errors[#result.errors + 1] = tostring(failure); break end
        result.added = result.added + batch.added
        result.existing = result.existing + batch.existing
        completed = completed + batch.completed
        for path in pairs(batch.seen) do seen[path] = true end
        for _, message in ipairs(batch.errors) do result.errors[#result.errors + 1] = message end
        if result.cancelled then break end
    end
    result.pending = #files - completed
    progress:done()
    return result
end

return Catalog
