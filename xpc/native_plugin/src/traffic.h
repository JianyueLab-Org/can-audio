// SPDX-FileCopyrightText: Copyright (C) 2013 swift Project Community / Contributors
// SPDX-FileCopyrightText: Copyright (C) 2026 XPC for CAN contributors
// SPDX-License-Identifier: GPL-3.0-or-later
//
// Traffic rendering for XPC for CAN, ported from swift's xswiftbus
// (src/xswiftbus/traffic.h, https://github.com/swift-project/pilotclient).
//
// What is swift's and kept as-is: aircraft are XPMP planes created from the
// ICAO/airline/livery triplet (or a model name), positions go through the
// three-sample interpolation in interpolatePosition(), gear is lerped over
// 5 s, and every frame ends with one XPMPUpdatePlanes() + XPMP_PrepListHook().
// What changed: there is no D-Bus. The XPC client feeds the same calls over
// the local UDP bridge (see bridge.cpp), and the follow-plane view, terrain
// probe service and settings file are left out.

#ifndef XPC_NATIVE_TRAFFIC_H
#define XPC_NATIVE_TRAFFIC_H

#include <XPLMCamera.h>
#include <XPLMDataAccess.h>
#include <XPLMDisplay.h>

#include <array>
#include <chrono>
#include <set>
#include <string>
#include <unordered_map>
#include <vector>

#include "XPMPMultiplayer.h"

namespace XpcNative
{
    //! One aircraft as the client describes it in a bridge frame.
    struct PlaneState
    {
        std::string callsign;
        std::string modelName;
        std::string aircraftIcao;
        std::string airlineIcao;
        std::string livery;

        bool hasPosition = false;
        double latitudeDeg = 0;
        double longitudeDeg = 0;
        double altitudeFt = 0;
        double pitchDeg = 0;
        double rollDeg = 0;
        double headingDeg = 0;
        bool onGround = false;

        double gear = 1;
        double flaps = 0;
        double spoilers = 0;
        double thrust = 0;
        bool hasLights = false;
        bool landLight = true;
        bool taxiLight = false;
        bool beaconLight = true;
        bool strobeLight = true;
        bool navLight = true;

        int squawk = 2000;
        bool modeC = true;
        bool ident = false;
    };

    class CTraffic
    {
    public:
        CTraffic();
        ~CTraffic();

        //! Initialize the multiplayer planes rendering and return true if successful
        bool initialize(const std::string &pluginDir);

        //! Grab X-Plane's AI planes for TCAS and start drawing
        bool acquireMultiplayerPlanes(std::string *owner = nullptr);

        //! Reverse the actions of initialize().
        void cleanup();

        //! Load every CSL package below the given folder; returns how many models are now known
        int loadPlanesPackage(const std::string &path);

        //! Set the ICAO code to use for aircraft without a model match
        void setDefaultIcao(const std::string &defaultIcao);

        //! Set whether the plugin draws callsign labels above aircraft
        void setDrawingLabels(bool drawing);
        bool isDrawingLabels() const { return m_drawLabels; }

        //! Introduce a new traffic aircraft
        void addPlane(const std::string &callsign, const std::string &modelName, const std::string &aircraftIcao,
                      const std::string &airlineIcao, const std::string &livery);

        //! Remove a traffic aircraft
        void removePlane(const std::string &callsign);

        //! Remove all traffic aircraft
        void removeAllPlanes();

        //! Apply one complete frame from the client: add, re-model, move and remove planes
        void applyFrame(const std::vector<PlaneState> &planes);

        //! Perform per-frame processing (interpolation and the XPMP update)
        int process();

        size_t planeCount() const { return m_planesByCallsign.size(); }

    private:
        //! Remote aircraft
        struct Plane
        {
            void *id = nullptr;
            std::string callsign;
            std::string aircraftIcao;
            std::string airlineIcao;
            std::string livery;
            std::string modelName;
            bool isOnGround = false;
            char label[32] {};
            XPMPPlaneSurfaces_t surfaces;
            float targetGearPosition = 0;
            std::chrono::steady_clock::time_point prevSurfacesLerpTime;
            std::chrono::steady_clock::time_point positionTimes[3];
            XPMPPlanePosition_t positions[4]; // 1 as input for extrapolation, 1 as next input, 1 latest, 1 as output
            XPMPPlaneSurveillance_t surveillance;
            Plane(void *id_, const std::string &callsign_, const std::string &aircraftIcao_,
                  const std::string &airlineIcao_, const std::string &livery_, const std::string &modelName_);
        };

        void setPlanePosition(Plane *plane, const PlaneState &state);
        void setPlaneSurfaces(Plane *plane, const PlaneState &state);
        void setPlaneTransponder(Plane *plane, const PlaneState &state);
        std::string resolveModelName(const std::string &modelName) const;

        void doPlaneUpdates();
        void interpolatePosition(Plane *);
        void interpolateGear(Plane *);

        static int drawLabels(XPLMDrawingPhase phase, int isBefore, void *refcon);
        void drawLabels();

        bool m_initialized = false;
        bool m_enabledMultiplayer = false;
        bool m_drawLabels = true;
        XPMPConfiguration_t m_configuration = {};

        std::set<std::string> m_loadedPackageRoots;
        std::unordered_map<std::string, std::string> m_modelStrings; // uppercase full name -> mixed case
        std::unordered_map<std::string, std::string> m_modelSuffixes; // uppercase OBJ8_AIRCRAFT name -> full name
        std::unordered_map<std::string, Plane *> m_planesByCallsign;
        std::unordered_map<void *, Plane *> m_planesById;
        std::vector<XPMPUpdate_t> m_updates;

        XPLMDataRef m_worldMat = nullptr;
        XPLMDataRef m_projMat = nullptr;
        XPLMDataRef m_windowWidth = nullptr;
        XPLMDataRef m_windowHeight = nullptr;
        XPLMDataRef m_viewX = nullptr;
        XPLMDataRef m_viewY = nullptr;
        XPLMDataRef m_viewZ = nullptr;
        std::array<float, 3> m_labelColor { { 1.0f, 0.75f, 0.0f } };
    };
} // namespace XpcNative

#endif // XPC_NATIVE_TRAFFIC_H
