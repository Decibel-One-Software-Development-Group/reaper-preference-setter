--[[
  SiRPS New Show Project
  Decibel One — https://decibelone.com/reaper-preference-setter/

  Creates a new project in its own dated folder, named from the same save-as
  pattern SiRPS sets, with the media folder already pointed at Audio/.

  Why this exists: REAPER applies its save-as wildcard pattern when it makes
  the project at launch, but not to File > New Project, so a show started
  mid-session lands unnamed. This does the naming itself.

  It never touches the file dialog, so it needs no extension — not SWS, not
  js_ReaScriptAPI. It reads REAPER's own settings, shows you the name to
  confirm or edit, then creates the folder and saves into it.

  Bind it to a key or a toolbar button and use it instead of File > New Project.
]]

local SEP = package.config:sub(1, 1)   -- "/" on macOS, "\" on Windows

local function cfg(name)
  local ok, value = reaper.get_config_var_string(name)
  if ok and value then return value end
  return ""
end

-- REAPER's wildcards, resolved the way REAPER resolves them.
-- $year2 must go before $year, or "$year2" becomes the full year with a
-- stray 2 on the end.
local function resolve(pattern)
  local now = os.date("*t")
  local subs = {
    { "%$year2",  string.format("%02d", now.year % 100) },
    { "%$year",   string.format("%04d", now.year)  },
    { "%$month",  string.format("%02d", now.month) },
    { "%$day",    string.format("%02d", now.day)   },
    { "%$hour",   string.format("%02d", now.hour)  },
    { "%$minute", string.format("%02d", now.min)   },
    { "%$second", string.format("%02d", now.sec)   },
  }
  local out = pattern or ""
  for _, s in ipairs(subs) do out = out:gsub(s[1], s[2]) end
  return out
end

-- Characters that are illegal in a filename, or that would send the project
-- into a different directory entirely.
local function sanitize(name)
  return (name:gsub('[<>:"/\\|?*]', ""):gsub("^%s+", ""):gsub("[%s%.]+$", ""))
end

local function main()
  local pattern  = cfg("projsaveaspattern")
  local savepath = cfg("defsavepath")
  local recpath  = cfg("projdefrecpath")

  if savepath == "" then
    reaper.ShowMessageBox(
      "No default save path is set.\n\n" ..
      "Set one in SiRPS, or in REAPER: Preferences > Paths >\n" ..
      '"Default path to save new projects".',
      "SiRPS New Show Project", 0)
    return
  end

  local suggested = resolve(pattern)
  if suggested == "" then suggested = resolve("$year-$month-$day_$hour$minute") end
  if recpath == "" then recpath = "Audio" end

  local ok, csv = reaper.GetUserInputs(
    "New Show Project", 2,
    "Project name:,Media folder:,extrawidth=180",
    suggested .. "," .. recpath)
  if not ok then return end

  local name, media = csv:match("^(.-),(.*)$")
  name  = sanitize(name or "")
  media = sanitize(media or "")
  if name == "" then
    reaper.ShowMessageBox("A project name is needed.", "SiRPS New Show Project", 0)
    return
  end

  local folder = savepath .. SEP .. name
  local rpp    = folder .. SEP .. name .. ".RPP"

  -- Never write over a project that is already there. The pattern carries
  -- minutes, so this only happens twice inside one minute — but losing a show
  -- to a silent overwrite is not a risk worth carrying.
  if reaper.file_exists(rpp) then
    reaper.ShowMessageBox(
      "This project already exists:\n\n" .. rpp .. "\n\nNothing was changed.",
      "SiRPS New Show Project", 0)
    return
  end

  reaper.RecursiveCreateDirectory(folder, 0)

  -- New project first: it resets project settings, so the record path has to
  -- be set after it, not before. This uses the default template if one is set,
  -- which is the point — the template brings the tracks, this brings the name.
  reaper.Main_OnCommand(40023, 0)   -- File: New project
  if media ~= "" then
    reaper.GetSetProjectInfo_String(0, "RECORD_PATH", media, true)
  end
  reaper.Main_SaveProjectEx(0, rpp, 0)

  reaper.ShowMessageBox(
    "Created:\n\n" .. rpp .. "\n\nRecording to: " ..
    (media ~= "" and (media .. SEP) or "the project folder"),
    "SiRPS New Show Project", 0)
end

main()
