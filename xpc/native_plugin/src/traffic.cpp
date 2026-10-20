// SPDX-FileCopyrightText: Copyright (C) 2013 swift Project Community / Contributors
// SPDX-FileCopyrightText: Copyright (C) 2026 XPC for CAN contributors
// SPDX-License-Identifier: GPL-3.0-or-later
//
// Ported from swift's xswiftbus (src/xswiftbus/traffic.cpp). The plane
// lifecycle, interpolation, gear lerp, labels and the XPMP update loop are
// swift's; the D-Bus message handler is replaced by applyFrame().

#ifndef NOMINMAX
#    define NOMINMAX
#endif

#include "traffic.h"
#include "utils.h"

#include <XPLMGraphics.h>
#include <XPLMPlanes.h>
#include <XPLMPlugin.h>
#include <XPLMProcessing.h>
#include <XPLMUtilities.h>

#include <algorithm>
#include <cassert>
#include <cctype>
#include <cstdlib>
#include <cmath>
#include <cstring>
#include <ctime>
#include <filesystem>
#include <limits>
#include <system_error>

float XPMP_PrepListHook(float, float, int, void *); // defined in xplanemp2/src/Renderer.cpp

using namespace std::chrono_literals;
namespace fs = std::filesystem;

namespace XpcNative
{
    namespace
    {
        std::string upper(std::string s)
        {
            std::transform(s.begin(), s.end(), s.begin(),
                           [](char c) { return static_cast<char>(std::toupper(static_cast<unsigned char>(c))); });
            return s;
        }

        fs::path utf8Path(const std::string &s)
        {
#if defined(__cpp_char8_t)
            return fs::path(std::u8string(s.begin(), s.end()));
#else
            return fs::u8path(s);
#endif
        }

        std::string pathString(const fs::path &p)
        {
            auto u8 = p.generic_u8string();
            return std::string(u8.begin(), u8.end());
        }
    } // namespace

    CTraffic::Plane::Plane(void *id_, const std::string &callsign_, const std::string &aircraftIcao_,
                           const std::string &airlineIcao_, const std::string &livery_, const std::string &modelName_)
        : id(id_), callsign(callsign_), aircraftIcao(aircraftIcao_), airlineIcao(airlineIcao_), livery(livery_),
          modelName(modelName_)
    {
        std::memset(static_cast<void *>(&positions), 0, sizeof(positions));
        for (auto &position : positions) { position.size = sizeof(position); }
        std::memset(static_cast<void *>(&surveillance), 0, sizeof(surveillance));
        surveillance.size = sizeof(surveillance);
        std::memset(static_cast<void *>(&surfaces), 0, sizeof(surfaces));
        surfaces.size = sizeof(surfaces);
        surfaces.lights.bcnLights = surfaces.lights.landLights = surfaces.lights.navLights =
            surfaces.lights.strbLights = 1;

        std::strncpy(label, callsign.c_str(), sizeof(label) - 1);
        for (auto &position : positions) { memcpy(position.label, label, sizeof(label)); }

        std::srand(static_cast<unsigned int>(std::time(nullptr)));
        surfaces.lights.timeOffset = static_cast<uint16_t>(std::rand() % 0xffff);
    }

    CTraffic::CTraffic()
    {
        m_worldMat = XPLMFindDataRef("sim/graphics/view/world_matrix");
        m_projMat = XPLMFindDataRef("sim/graphics/view/projection_matrix_3d");
        m_windowWidth = XPLMFindDataRef("sim/graphics/view/window_width");
        m_windowHeight = XPLMFindDataRef("sim/graphics/view/window_height");
        m_viewX = XPLMFindDataRef("sim/graphics/view/view_x");
        m_viewY = XPLMFindDataRef("sim/graphics/view/view_y");
        m_viewZ = XPLMFindDataRef("sim/graphics/view/view_z");
        XPLMRegisterDrawCallback(&CTraffic::drawLabels, xplm_Phase_Window, 0, this);
    }

    CTraffic::~CTraffic()
    {
        XPLMUnregisterDrawCallback(&CTraffic::drawLabels, xplm_Phase_Window, 0, this);
        cleanup();
    }

    bool CTraffic::initialize(const std::string &pluginDir)
    {
        if (!m_initialized)
        {
            const std::string dir = pluginDir + "/LegacyData/";
            const std::string related = dir + "related.txt";
            const std::string doc8643 = dir + "Doc8643.txt";

            m_configuration.maxFullAircraftRenderingDistance = 100.0f;
            m_configuration.enableSurfaceClamping = true;
            m_configuration.debug.modelMatching = false;
            auto err = XPMPMultiplayerInit(&m_configuration, related.c_str(), doc8643.c_str());
            if (err && *err)
            {
                ERROR_LOG(std::string("multiplayer init failed: ") + err);
                cleanup();
            }
            else { m_initialized = true; }
        }
        return m_initialized;
    }

    bool CTraffic::acquireMultiplayerPlanes(std::string *owner)
    {
        if (!m_enabledMultiplayer)
        {
            auto err = XPMPMultiplayerEnable();
            if (err && *err) { ERROR_LOG(std::string("could not enable multiplayer: ") + err); }
            else
            {
                m_enabledMultiplayer = true;

                // we will call xplanemp's callback from within our own callback
                XPLMUnregisterFlightLoopCallback(XPMP_PrepListHook, nullptr);
            }
        }

        int totalAircraft = 0;
        int activeAircraft = 0;
        XPLMPluginID controller = XPLM_NO_PLUGIN_ID;
        XPLMCountAircraft(&totalAircraft, &activeAircraft, &controller);

        char pluginName[256] {};
        if (controller != XPLM_NO_PLUGIN_ID) { XPLMGetPluginInfo(controller, pluginName, nullptr, nullptr, nullptr); }
        if (owner) { *owner = std::string(pluginName); }
        return m_enabledMultiplayer;
    }

    void CTraffic::cleanup()
    {
        removeAllPlanes();

        if (m_enabledMultiplayer)
        {
            m_enabledMultiplayer = false;
            XPMPMultiplayerDisable();
        }

        if (m_initialized)
        {
            m_initialized = false;
            XPMPMultiplayerCleanup();
        }
    }

    int CTraffic::loadPlanesPackage(const std::string &path)
    {
        if (path.empty()) { return XPMPGetNumberOfInstalledModels(); }

        // xplanemp2 loads the immediate subfolders of the folder it is given
        // as packages. The client's CSL setting may point at the package
        // itself or at any parent of several, so collect every folder that
        // holds an xsb_aircraft.txt and hand xplanemp2 their parents.
        std::set<std::string> roots;
        std::error_code ec;
        const fs::path base = utf8Path(path);
        if (fs::is_regular_file(base / "xsb_aircraft.txt", ec)) { roots.insert(pathString(base.parent_path())); }
        if (fs::is_directory(base, ec))
        {
            for (fs::recursive_directory_iterator it(base, fs::directory_options::skip_permission_denied, ec), end;
                 it != end; it.increment(ec))
            {
                if (ec) { break; }
                if (it.depth() > 4) { it.disable_recursion_pending(); }
                if (it->is_regular_file(ec) && upper(pathString(it->path().filename())) == "XSB_AIRCRAFT.TXT")
                {
                    roots.insert(pathString(it->path().parent_path().parent_path()));
                }
            }
        }

        for (const auto &root : roots)
        {
            if (!m_loadedPackageRoots.insert(root).second) { continue; }
            auto err = XPMPMultiplayerLoadCSLPackages(root.c_str());
            if (err && *err) { WARNING_LOG("problems loading CSL packages from " + root + ": " + err); }
            else { INFO_LOG("loaded CSL packages from " + root); }
        }

        m_modelStrings.clear();
        m_modelSuffixes.clear();
        for (int i = 0, end = XPMPGetNumberOfInstalledModels(); i < end; ++i)
        {
            const char *mixedcase = nullptr;
            XPMPGetModelInfo(i, &mixedcase, nullptr, nullptr, nullptr);
            if (!mixedcase) { continue; }
            const std::string name(mixedcase);
            m_modelStrings[upper(name)] = name;
            // xplanemp2 names a model "<package folder> <OBJ8_AIRCRAFT name>";
            // the client only knows the second half.
            const auto space = name.find_last_of(' ');
            const std::string suffix = upper(space == std::string::npos ? name : name.substr(space + 1));
            m_modelSuffixes.emplace(suffix, name);
        }
        if (roots.empty()) { WARNING_LOG("no CSL package (xsb_aircraft.txt) found below " + path); }
        return XPMPGetNumberOfInstalledModels();
    }

    void CTraffic::setDefaultIcao(const std::string &defaultIcao) { XPMPSetDefaultPlaneICAO(defaultIcao.c_str()); }

    void CTraffic::setDrawingLabels(bool drawing) { m_drawLabels = drawing; }

    std::string CTraffic::resolveModelName(const std::string &modelName) const
    {
        if (modelName.empty()) { return {}; }
        const std::string key = upper(modelName);
        auto it = m_modelStrings.find(key);
        if (it != m_modelStrings.end()) { return it->second; }
        auto suffix = m_modelSuffixes.find(key);
        if (suffix != m_modelSuffixes.end()) { return suffix->second; }
        return {};
    }

    void CTraffic::addPlane(const std::string &callsign, const std::string &modelName, const std::string &aircraftIcao,
                            const std::string &airlineIcao, const std::string &livery)
    {
        auto planeIt = m_planesByCallsign.find(callsign);
        if (planeIt != m_planesByCallsign.end()) { return; }

        const std::string icao = aircraftIcao.empty() ? std::string("????") : aircraftIcao;
        const std::string resolved = resolveModelName(modelName);
        XPMPPlaneID id = nullptr;
        if (resolved.empty())
        {
            DEBUG_LOG("Model " + modelName + " is unknown, falling back to basic xpmp2 model matching");
            id = XPMPCreatePlane(icao.c_str(), airlineIcao.c_str(), livery.c_str());
        }
        else
        {
            id = XPMPCreatePlaneWithModelName(resolved.c_str(), icao.c_str(), airlineIcao.c_str(), livery.c_str());
        }

        if (!id)
        {
            WARNING_LOG("could not add " + callsign + " (" + icao + "/" + airlineIcao + ")");
            return;
        }

        Plane *plane = new Plane(id, callsign, aircraftIcao, airlineIcao, livery, modelName);
        m_planesByCallsign[callsign] = plane;
        m_planesById[id] = plane;
        INFO_LOG("added " + callsign + " (" + icao + "/" + (airlineIcao.empty() ? "-" : airlineIcao) + ") as " +
                 (resolved.empty() ? std::string("best xpmp match") : resolved));
    }

    void CTraffic::removePlane(const std::string &callsign)
    {
        auto planeIt = m_planesByCallsign.find(callsign);
        if (planeIt == m_planesByCallsign.end()) { return; }

        Plane *plane = planeIt->second;
        m_planesByCallsign.erase(planeIt);
        m_planesById.erase(plane->id);
        XPMPDestroyPlane(plane->id);
        delete plane;
    }

    void CTraffic::removeAllPlanes()
    {
        for (const auto &kv : m_planesByCallsign)
        {
            Plane *plane = kv.second;
            assert(plane);
            XPMPDestroyPlane(plane->id);
            delete plane;
        }
        m_planesByCallsign.clear();
        m_planesById.clear();
    }

    void CTraffic::applyFrame(const std::vector<PlaneState> &planes)
    {
        if (!m_initialized) { return; }

        std::set<std::string> wanted;
        for (const auto &state : planes) { wanted.insert(state.callsign); }

        std::vector<std::string> gone;
        for (const auto &kv : m_planesByCallsign)
        {
            if (!wanted.count(kv.first)) { gone.push_back(kv.first); }
        }
        for (const auto &callsign : gone) { removePlane(callsign); }

        for (const auto &state : planes)
        {
            if (state.callsign.empty()) { continue; }
            auto it = m_planesByCallsign.find(state.callsign);
            // swift removes and re-adds an aircraft whose model changed;
            // the #SB reply turns a stand-in into the real type this way.
            if (it != m_planesByCallsign.end())
            {
                const Plane *p = it->second;
                if (p->aircraftIcao != state.aircraftIcao || p->airlineIcao != state.airlineIcao ||
                    p->livery != state.livery || p->modelName != state.modelName)
                {
                    removePlane(state.callsign);
                    it = m_planesByCallsign.end();
                }
            }
            if (it == m_planesByCallsign.end())
            {
                addPlane(state.callsign, state.modelName, state.aircraftIcao, state.airlineIcao, state.livery);
                it = m_planesByCallsign.find(state.callsign);
                if (it == m_planesByCallsign.end()) { continue; }
            }
            Plane *plane = it->second;
            if (state.hasPosition) { setPlanePosition(plane, state); }
            setPlaneSurfaces(plane, state);
            setPlaneTransponder(plane, state);
        }
    }

    void CTraffic::setPlanePosition(Plane *plane, const PlaneState &state)
    {
        const bool first = plane->positionTimes[2].time_since_epoch().count() == 0;
        plane->positions[2].lat = state.latitudeDeg;
        plane->positions[2].lon = state.longitudeDeg;
        plane->positions[2].elevation = state.altitudeFt;
        plane->positions[2].pitch = static_cast<float>(state.pitchDeg);
        plane->positions[2].roll = static_cast<float>(state.rollDeg);
        plane->positions[2].heading = static_cast<float>(state.headingDeg);
        plane->positions[2].offsetScale = 1.0f;
        plane->positions[2].clampToGround = true;
        plane->positionTimes[2] = std::chrono::steady_clock::now();

        if (first)
        {
            // Nothing to interpolate from yet: start the history here so the
            // first frame is drawn where the aircraft is, not at 0/0.
            for (int i = 0; i < 2; ++i)
            {
                plane->positionTimes[i] = plane->positionTimes[2];
                std::memcpy(&plane->positions[i], &plane->positions[2], sizeof(plane->positions[0]));
            }
            std::memcpy(&plane->positions[3], &plane->positions[2], sizeof(plane->positions[0]));
        }

        // save 2 positions at 1-second intervals for use in interpolation
        if (plane->positionTimes[2] - plane->positionTimes[1] > 1s)
        {
            plane->positionTimes[0] = plane->positionTimes[1];
            plane->positionTimes[1] = plane->positionTimes[2];
            std::memcpy(&plane->positions[0], &plane->positions[1], sizeof(plane->positions[0]));
            std::memcpy(&plane->positions[1], &plane->positions[2], sizeof(plane->positions[0]));
        }

        plane->isOnGround = state.onGround;
    }

    void CTraffic::setPlaneSurfaces(Plane *plane, const PlaneState &state)
    {
        plane->targetGearPosition = static_cast<float>(state.gear);
        if (plane->prevSurfacesLerpTime.time_since_epoch().count() == 0)
        {
            plane->surfaces.gearPosition = plane->targetGearPosition;
            plane->prevSurfacesLerpTime = std::chrono::steady_clock::now();
        }
        plane->surfaces.flapRatio = static_cast<float>(state.flaps);
        plane->surfaces.slatRatio = static_cast<float>(state.flaps);
        plane->surfaces.spoilerRatio = static_cast<float>(state.spoilers);
        plane->surfaces.speedBrakeRatio = static_cast<float>(state.spoilers);
        plane->surfaces.thrust = static_cast<float>(state.thrust);
        if (state.hasLights)
        {
            plane->surfaces.lights.landLights = state.landLight;
            plane->surfaces.lights.taxiLights = state.taxiLight;
            plane->surfaces.lights.bcnLights = state.beaconLight;
            plane->surfaces.lights.strbLights = state.strobeLight;
            plane->surfaces.lights.navLights = state.navLight;
        }
    }

    void CTraffic::setPlaneTransponder(Plane *plane, const PlaneState &state)
    {
        plane->surveillance.code = state.squawk;
        if (state.ident) { plane->surveillance.mode = xpmpTransponderMode_ModeC_Ident; }
        else if (state.modeC) { plane->surveillance.mode = xpmpTransponderMode_ModeC; }
        else { plane->surveillance.mode = xpmpTransponderMode_Standby; }
    }

    int CTraffic::process()
    {
        if (m_initialized && m_enabledMultiplayer) { doPlaneUpdates(); }
        return 1;
    }

    void CTraffic::doPlaneUpdates()
    {
        m_updates.clear();
        for (const auto &pair : m_planesById)
        {
            Plane *plane = pair.second;
            if (plane->positionTimes[2].time_since_epoch().count() == 0) { continue; } // never positioned
            interpolatePosition(plane);
            interpolateGear(plane);
            XPMPUpdate_t update = { plane->id, &plane->positions[3], &plane->surfaces, &plane->surveillance,
                                    plane->isOnGround };
            m_updates.push_back(update);
        }
        if (!m_updates.empty()) { XPMPUpdatePlanes(m_updates.data(), sizeof(XPMPUpdate_t), m_updates.size()); }

        XPMP_PrepListHook(0, 0, 0, nullptr);
    }

    void CTraffic::interpolatePosition(Plane *plane)
    {
        std::memcpy(&plane->positions[3], &plane->positions[2], sizeof(plane->positions[2]));

        const auto now = std::chrono::steady_clock::now();
        const auto t1 = plane->positionTimes[2] - plane->positionTimes[0];
        const auto t2 = now - plane->positionTimes[0];

        // This interpolation is only intended to smooth over
        // small errors. Give up if the error is too large.
        if (t1 > 3s || t2 > 3s) { return; }
        if (t1.count() <= 0) { return; }

        const double dLat = plane->positions[2].lat - plane->positions[0].lat;
        const double dLon = plane->positions[2].lon - plane->positions[0].lon;
        const double dAlt = plane->positions[2].elevation - plane->positions[0].elevation;
        const double ratio = std::chrono::duration<double>(t2) / std::chrono::duration<double>(t1);

        plane->positions[3].lat = plane->positions[0].lat + dLat * ratio;
        plane->positions[3].lon = plane->positions[0].lon + dLon * ratio;
        plane->positions[3].elevation = plane->positions[0].elevation + dAlt * ratio;
    }

    void CTraffic::interpolateGear(Plane *plane)
    {
        const auto now = std::chrono::steady_clock::now();
        static const float epsilon = std::numeric_limits<float>::epsilon();
        const float f = plane->surfaces.gearPosition - plane->targetGearPosition;
        if (std::abs(f) > epsilon)
        {
            constexpr float gearMoveTimeMs = 5000;
            const auto gearPositionDiffRemaining = plane->targetGearPosition - plane->surfaces.gearPosition;

            const auto diffMs =
                std::chrono::duration_cast<std::chrono::milliseconds>(now - plane->prevSurfacesLerpTime);
            const auto gearPositionDiffThisFrame = (diffMs.count()) / gearMoveTimeMs;
            plane->surfaces.gearPosition += std::copysign(gearPositionDiffThisFrame, gearPositionDiffRemaining);
            plane->surfaces.gearPosition = std::max(0.0f, std::min(plane->surfaces.gearPosition, 1.0f));
        }
        plane->prevSurfacesLerpTime = now;
    }

    int CTraffic::drawLabels(XPLMDrawingPhase, int, void *refcon)
    {
        auto *traffic = static_cast<CTraffic *>(refcon);
        if (traffic && traffic->m_drawLabels) { traffic->drawLabels(); }
        return 1;
    }

    void CTraffic::drawLabels()
    {
        if (!m_worldMat || !m_projMat || !m_windowWidth || !m_windowHeight) { return; }

        static const double maxRangeM = 10000;
        static const double metersPerFt = 0.3048;
        std::array<float, 16> worldMat {};
        std::array<float, 16> projMat {};
        XPLMGetDatavf(m_worldMat, worldMat.data(), 0, 16);
        XPLMGetDatavf(m_projMat, projMat.data(), 0, 16);
        const double windowWidth = static_cast<double>(XPLMGetDatai(m_windowWidth));
        const double windowHeight = static_cast<double>(XPLMGetDatai(m_windowHeight));
        const double viewX = m_viewX ? XPLMGetDataf(m_viewX) : 0.0;
        const double viewY = m_viewY ? XPLMGetDataf(m_viewY) : 0.0;
        const double viewZ = m_viewZ ? XPLMGetDataf(m_viewZ) : 0.0;

        auto matrixMultVec = [](double out[4], const float m[16], const double v[4]) {
            out[0] = v[0] * m[0] + v[1] * m[4] + v[2] * m[8] + v[3] * m[12];
            out[1] = v[0] * m[1] + v[1] * m[5] + v[2] * m[9] + v[3] * m[13];
            out[2] = v[0] * m[2] + v[1] * m[6] + v[2] * m[10] + v[3] * m[14];
            out[3] = v[0] * m[3] + v[1] * m[7] + v[2] * m[11] + v[3] * m[15];
        };

        for (const auto &pair : m_planesById)
        {
            char *text = const_cast<char *>(pair.second->label);
            const XPMPPlanePosition_t &planePos = pair.second->positions[3];
            if (pair.second->positionTimes[2].time_since_epoch().count() == 0) { continue; }

            double worldPos[4] { 0, 0, 0, 1 };
            double localPos[4] {};
            double windowPos[4] {};
            XPLMWorldToLocal(planePos.lat, planePos.lon, planePos.elevation * metersPerFt, &worldPos[0], &worldPos[1],
                             &worldPos[2]);
            const double dx = viewX - worldPos[0];
            const double dy = viewY - worldPos[1];
            const double dz = viewZ - worldPos[2];
            if (dx * dx + dy * dy + dz * dz > maxRangeM * maxRangeM) { continue; }
            matrixMultVec(localPos, worldMat.data(), worldPos);
            matrixMultVec(windowPos, projMat.data(), localPos);
            if (std::abs(windowPos[3]) < 1e-9) { continue; }

            windowPos[3] = 1.0 / windowPos[3];
            windowPos[0] *= windowPos[3];
            windowPos[1] *= windowPos[3];
            windowPos[2] *= windowPos[3];

            if (windowPos[2] < 0.0 || windowPos[2] > 1.0)
            {
                continue; // plane is behind camera
            }
            XPLMDrawString(m_labelColor.data(), static_cast<int>(std::lround(windowWidth * (windowPos[0] * 0.5 + 0.5))),
                           static_cast<int>(std::lround(windowHeight * (windowPos[1] * 0.5 + 0.5))), text, nullptr,
                           xplmFont_Basic);
        }
    }
} // namespace XpcNative
