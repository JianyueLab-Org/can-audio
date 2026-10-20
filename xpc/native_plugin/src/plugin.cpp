// SPDX-FileCopyrightText: Copyright (C) 2013 swift Project Community / Contributors
// SPDX-FileCopyrightText: Copyright (C) 2026 XPC for CAN contributors
// SPDX-License-Identifier: GPL-3.0-or-later
//
// XPC for CAN native X-Plane plugin: plugin entry points.
//
// Laid out like swift's xswiftbus (main.cpp + plugin.cpp): the traffic object
// lives from XPluginEnable to XPluginDisable, and one flight loop drives it.
// Where xswiftbus pumps D-Bus in that loop, this one drains the UDP bridge.
// No Python, XPPython3 or other helper plugin is involved.

#ifndef NOMINMAX
#    define NOMINMAX
#endif

#include "bridge.h"
#include "traffic.h"
#include "utils.h"

#include <XPLMMenus.h>
#include <XPLMPlugin.h>
#include <XPLMProcessing.h>
#include <XPLMUtilities.h>

#include <cstring>
#include <memory>
#include <string>

namespace
{
    //! Matches the Python plugin's HEARTBEAT_TIMEOUT: the client pushes at least once a second
    constexpr double kHeartbeatTimeout = 30.0;
    constexpr const char *kDefaultIcao = "A320";

    std::unique_ptr<XpcNative::CTraffic> g_traffic;
    std::unique_ptr<XpcNative::CBridge> g_bridge;
    std::string g_pluginDir;
    double g_lastFrame = -1.0;
    std::vector<std::string> g_cslPaths;

    XPLMMenuID g_menu = nullptr;
    int g_labelsItem = -1;
    int g_parentItem = -1;

    std::string pluginDir()
    {
        char path[1024] {};
        XPLMGetPluginInfo(XPLMGetMyID(), nullptr, path, nullptr, nullptr);
        std::string dir(path);
        for (auto &c : dir)
        {
            if (c == '\\') { c = '/'; }
        }
        // <X-Plane>/Resources/plugins/XPC_for_CAN/64/win.xpl -> .../XPC_for_CAN
        for (int i = 0; i < 2; ++i)
        {
            const auto slash = dir.find_last_of('/');
            if (slash == std::string::npos) { break; }
            dir.erase(slash);
        }
        return dir;
    }

    void updateLabelsMenu()
    {
        if (g_menu && g_labelsItem >= 0 && g_traffic)
        {
            XPLMCheckMenuItem(g_menu, g_labelsItem,
                              g_traffic->isDrawingLabels() ? xplm_Menu_Checked : xplm_Menu_Unchecked);
        }
    }

    void menuHandler(void *, void *item)
    {
        if (!g_traffic) { return; }
        if (reinterpret_cast<intptr_t>(item) == 1)
        {
            g_traffic->setDrawingLabels(!g_traffic->isDrawingLabels());
            updateLabelsMenu();
        }
    }

    void loadCsl(const std::vector<std::string> &paths)
    {
        if (paths == g_cslPaths) { return; }
        g_cslPaths = paths;
        const int before = XPMPGetNumberOfInstalledModels();
        int after = before;
        for (const auto &path : paths) { after = g_traffic->loadPlanesPackage(path); }
        if (after != before)
        {
            INFO_LOG(std::to_string(after) + " CSL models available; re-matching every aircraft");
            // Planes created before the new models existed got a stand-in.
            // They come back on the next frame and are matched again.
            g_traffic->removeAllPlanes();
        }
    }

    float flightLoop(float, float, int, void *)
    {
        if (!g_traffic) { return -1.0f; }
        const double now = XPLMGetElapsedTime();

        if (g_bridge)
        {
            if (auto frame = g_bridge->poll())
            {
                if (!frame->defaultIcao.empty()) { g_traffic->setDefaultIcao(frame->defaultIcao); }
                if (!frame->cslPaths.empty()) { loadCsl(frame->cslPaths); }
                if (frame->labels)
                {
                    g_traffic->setDrawingLabels(*frame->labels);
                    updateLabelsMenu();
                }
                g_traffic->applyFrame(frame->planes);
                g_lastFrame = now;
            }
        }

        if (g_lastFrame >= 0 && now - g_lastFrame > kHeartbeatTimeout && g_traffic->planeCount())
        {
            INFO_LOG("no frame from the XPC client for 30 s, removing all traffic");
            g_traffic->removeAllPlanes();
            g_lastFrame = -1.0;
        }

        g_traffic->process();
        return -1.0f;
    }
} // namespace

PLUGIN_API int XPluginStart(char *o_name, char *o_sig, char *o_desc)
{
    XPLMEnableFeature("XPLM_USE_NATIVE_PATHS", 1);
    std::strcpy(o_name, "XPC for CAN");
    std::strcpy(o_sig, "net.ceruleanavi.xpc.traffic");
    std::strcpy(o_desc, "Draws Cerulean Aviation Network traffic for XPC for CAN");
    // The marker is what xpinstall.protocol_version() finds in this file;
    // logging it also keeps the linker from dropping the string.
    INFO_LOG(std::string("plugin starting (") + XPC_BRIDGE_PROTOCOL_MARKER + ")");
    return 1;
}

PLUGIN_API void XPluginStop() {}

PLUGIN_API int XPluginEnable()
{
    g_pluginDir = pluginDir();
    g_traffic = std::make_unique<XpcNative::CTraffic>();
    if (!g_traffic->initialize(g_pluginDir))
    {
        ERROR_LOG("could not initialise the renderer; is LegacyData/ next to the 64/ folder in " + g_pluginDir + "?");
    }
    g_traffic->setDefaultIcao(kDefaultIcao);
    // CSL packages the installer copied next to the plugin
    g_traffic->loadPlanesPackage(g_pluginDir + "/CSL");
    g_cslPaths.clear();

    std::string owner;
    if (!g_traffic->acquireMultiplayerPlanes(&owner) || (!owner.empty() && owner != "XPC for CAN"))
    {
        WARNING_LOG("AI aircraft are held by '" + owner + "'; traffic is drawn but TCAS may not show it");
    }

    g_bridge = std::make_unique<XpcNative::CBridge>();
    g_lastFrame = -1.0;

    XPLMMenuID plugins = XPLMFindPluginsMenu();
    if (plugins)
    {
        g_parentItem = XPLMAppendMenuItem(plugins, "XPC for CAN", nullptr, 0);
        g_menu = XPLMCreateMenu("XPC for CAN", plugins, g_parentItem, menuHandler, nullptr);
        if (g_menu) { g_labelsItem = XPLMAppendMenuItem(g_menu, "Show callsign labels", reinterpret_cast<void *>(1), 0); }
        updateLabelsMenu();
    }

    XPLMRegisterFlightLoopCallback(flightLoop, -1.0f, nullptr);
    return 1;
}

PLUGIN_API void XPluginDisable()
{
    XPLMUnregisterFlightLoopCallback(flightLoop, nullptr);
    if (g_menu) { XPLMDestroyMenu(g_menu); }
    if (g_parentItem >= 0) { XPLMRemoveMenuItem(XPLMFindPluginsMenu(), g_parentItem); }
    g_menu = nullptr;
    g_labelsItem = -1;
    g_parentItem = -1;
    g_bridge.reset();
    g_traffic.reset();
}

PLUGIN_API void XPluginReceiveMessage(XPLMPluginID, int, void *) {}
