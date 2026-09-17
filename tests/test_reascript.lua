-- Exercises reascripts/SiRPS New Show Project.lua against a fake REAPER API.
-- Run:  lua tests/test_reascript.lua        (from the repo root)
-- The Python suite runs this too when `lua` is on PATH.

local SCRIPT = "reascripts/SiRPS New Show Project.lua"
local failures = 0

local function check(name, got, want)
  if got ~= want then
    failures = failures + 1
    print(string.format("FAIL  %s\n        got:  %s\n        want: %s",
                        name, tostring(got), tostring(want)))
  else
    print("ok    " .. name)
  end
end

-- cfg: config values; accept: what the user does in the dialog (false = cancel,
-- or a replacement CSV); exists: whether the .RPP is already there.
local function run(cfg, accept, exists)
  local calls = {}
  reaper = {
    get_config_var_string = function(n) return cfg[n] ~= nil, cfg[n] or "" end,
    GetUserInputs = function(_, _, _, defaults)
      calls.prefilled = defaults
      if accept == false then return false, "" end
      return true, (accept == true) and defaults or accept
    end,
    file_exists = function() return exists == true end,
    RecursiveCreateDirectory = function(p) calls.mkdir = p end,
    Main_OnCommand = function(id) calls.command = id end,
    GetSetProjectInfo_String = function(_, k, v) calls[k] = v end,
    Main_SaveProjectEx = function(_, f) calls.saved = f end,
    ShowMessageBox = function(m) calls.msg = (calls.msg or "") .. m end,
  }
  dofile(SCRIPT)
  return calls
end

local FULL = { projsaveaspattern = "MHET_$year-$month-$day_$hour$minute",
               defsavepath = "/Shows", projdefrecpath = "Audio" }
local stamp = os.date("%Y-%m-%d_%H%M")

-- the happy path
local c = run(FULL, true, false)
check("dialog is pre-filled with the resolved name",
      c.prefilled, "MHET_" .. stamp .. ",Audio")
check("creates the dated folder", c.mkdir, "/Shows/MHET_" .. stamp)
check("invokes File: New project", c.command, 40023)
check("sets the record path", c.RECORD_PATH, "Audio")
check("saves into the folder", c.saved,
      "/Shows/MHET_" .. stamp .. "/MHET_" .. stamp .. ".RPP")

-- cancelling must change nothing at all
c = run(FULL, false, false)
check("cancel creates no folder", c.mkdir, nil)
check("cancel starts no project", c.command, nil)
check("cancel saves nothing", c.saved, nil)

-- an existing project is never overwritten
c = run(FULL, true, true)
check("existing project is not overwritten", c.saved, nil)
check("existing project starts no new one", c.command, nil)
check("existing project says so", (c.msg or ""):find("already exists") ~= nil, true)

-- no save path configured
c = run({ projsaveaspattern = "X" }, true, false)
check("no save path: nothing created", c.mkdir, nil)
check("no save path: says which setting", (c.msg or ""):find("save path") ~= nil, true)

-- a name the user edits to something with a path separator in it
c = run(FULL, "MH/ET:v2,Audio", false)
check("a slash cannot redirect the save", c.saved, "/Shows/MHETv2/MHETv2.RPP")

-- no pattern set at all still suggests something usable
c = run({ defsavepath = "/Shows" }, true, false)
check("falls back to a dated name", c.prefilled, stamp .. ",Audio")

-- an empty media folder records into the project folder, not a stray one
c = run(FULL, "Show," , false)
check("blank media folder sets no record path", c.RECORD_PATH, nil)
check("blank media folder still saves", c.saved, "/Shows/Show/Show.RPP")

if failures > 0 then
  print(string.format("\n%d check(s) failed", failures))
  os.exit(1)
end
print("\nall checks passed")
