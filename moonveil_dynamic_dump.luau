-- MoonVeil dynamic string-dump harness (run in a Luau/Lua 5.2+ environment, e.g. Roblox Studio console)
-- Usage: put the obfuscated script's source in a ModuleScript / variable `src`, then:
--   local dump = require(path.to.this)(src)   -- returns { strings = {...}, calls = {...} }

local function dump(src)
    local captured = { strings = {}, calls = {} }

    -- 1) Wrap string methods so the decoder's f:u / f.h / f.i style calls are recorded
    local realString = string
    local mt = getmetatable("")
    local oldIndex = mt.__index
    mt.__index = function(s, k)
        local v = oldIndex[k]
        if type(v) == "function" then
            return function(self, ...)
                local r = v(self, ...)
                if k ~= "sub" and k ~= "byte" and k ~= "len" then
                    captured.calls[#captured.calls + 1] = {
                        method = "string." .. tostring(k),
                        arg = tostring(self):sub(1, 120),
                        result = tostring(r):sub(1, 200),
                    }
                end
                return r
            end
        end
        return v
    end

    -- 2) Proxy env: record every function/method the VM grabs
    local env = setmetatable({}, {
        __index = function(_, k)
            local v = _G[k]
            if type(v) == "function" then
                return function(...)
                    captured.calls[#captured.calls + 1] =
                        { method = tostring(k), arg = "", result = "" }
                    return v(...)
                end
            end
            return v
        end,
    })

    -- 3) Load & run, catching errors (fine: strings are usually decoded before any crash)
    local fn, err = (loadstring or load)(src, "obf", "t", env)
    if fn then
        local ok, res = pcall(fn)
        captured.returned = ok and tostring(res) or ("runtime error: " .. tostring(res))
    else
        captured.load_error = err
    end

    mt.__index = oldIndex  -- restore
    return captured
end

return dump
