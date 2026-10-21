// SPDX-FileCopyrightText: Copyright (C) 2026 XPC for CAN contributors
// SPDX-License-Identifier: GPL-3.0-or-later

#ifndef NOMINMAX
#    define NOMINMAX
#endif

// winsock2.h must come before anything that pulls in <windows.h>: XPLMDefs.h
// (via bridge.h -> traffic.h) includes it, which drags in the old winsock.h
// and makes winsock2.h redefine every socket type.
#ifdef _WIN32
#    include <winsock2.h>
#    include <ws2tcpip.h>
#else
#    include <arpa/inet.h>
#    include <fcntl.h>
#    include <netinet/in.h>
#    include <sys/socket.h>
#    include <unistd.h>
#endif

#include "bridge.h"
#include "utils.h"

#include <nlohmann/json.hpp>

#include <algorithm>
#include <cmath>

using json = nlohmann::json;

namespace XpcNative
{
    namespace
    {
#ifdef _WIN32
        constexpr uintptr_t kInvalidSocket = static_cast<uintptr_t>(INVALID_SOCKET);
#else
        constexpr int kInvalidSocket = -1;
#endif

        std::string base64Decode(const std::string &input)
        {
            static const std::string chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
            std::string output;
            int value = 0;
            int bits = -8;
            for (unsigned char c : input)
            {
                const auto index = chars.find(static_cast<char>(c));
                if (index == std::string::npos) { continue; } // '=' padding and anything else
                value = (value << 6) + static_cast<int>(index);
                bits += 6;
                if (bits >= 0)
                {
                    output.push_back(static_cast<char>((value >> bits) & 0xff));
                    bits -= 8;
                }
            }
            return output;
        }

        std::string text(const json &j, const char *key)
        {
            auto it = j.find(key);
            if (it == j.end() || !it->is_string()) { return {}; }
            return it->get<std::string>();
        }

        double number(const json &j, const char *key, double fallback)
        {
            auto it = j.find(key);
            if (it == j.end()) { return fallback; }
            if (it->is_number()) { return it->get<double>(); }
            if (it->is_boolean()) { return it->get<bool>() ? 1.0 : 0.0; }
            return fallback;
        }

        std::optional<bool> flag(const json &j, const char *key)
        {
            auto it = j.find(key);
            if (it == j.end() || it->is_null()) { return std::nullopt; }
            if (it->is_boolean()) { return it->get<bool>(); }
            if (it->is_number()) { return it->get<double>() > 0.5; }
            return std::nullopt;
        }

        bool sixNumbers(const json &j, const char *key, double out[6])
        {
            auto it = j.find(key);
            if (it == j.end() || !it->is_array() || it->size() < 6) { return false; }
            for (size_t i = 0; i < 6; ++i)
            {
                if (!(*it)[i].is_number()) { return false; }
                out[i] = (*it)[i].get<double>();
                if (!std::isfinite(out[i])) { return false; }
            }
            return true;
        }

        PlaneState planeFrom(const json &entry)
        {
            PlaneState s;
            s.callsign = text(entry, "callsign");
            s.modelName = text(entry, "model");
            s.aircraftIcao = text(entry, "equipment");
            s.airlineIcao = text(entry, "airline");
            s.livery = text(entry, "livery");

            // "pose" is the client-interpolated state - what swift's own
            // interpolator hands xswiftbus. "target" (the last reported
            // sample) is the fallback for an older client.
            double pose[6] {};
            if (sixNumbers(entry, "pose", pose) || sixNumbers(entry, "target", pose))
            {
                s.hasPosition = true;
                s.latitudeDeg = pose[0];
                s.longitudeDeg = pose[1];
                s.altitudeFt = pose[2];
                s.pitchDeg = pose[3];
                s.rollDeg = pose[4];
                s.headingDeg = pose[5];
            }
            s.onGround = flag(entry, "on_ground").value_or(false);

            // No gear info yet (no config packet from the sender): guess the
            // way swift does for aircraft without parts - down on or near the
            // ground, up otherwise.
            const auto gear = flag(entry, "gear_down");
            if (gear) { s.gear = *gear ? 1.0 : 0.0; }
            else
            {
                const double agl = number(entry, "agl", std::nan(""));
                s.gear = (s.onGround || (std::isfinite(agl) && agl < 1500.0)) ? 1.0 : 0.0;
            }
            s.flaps = std::max(0.0, std::min(1.0, number(entry, "flaps", 0.0)));
            s.spoilers = flag(entry, "spoilers").value_or(false) ? 1.0 : 0.0;
            s.thrust = flag(entry, "engines_on").value_or(true) ? 1.0 : 0.0;

            auto lights = entry.find("lights");
            if (lights != entry.end() && lights->is_object() && !lights->empty())
            {
                s.hasLights = true;
                s.landLight = flag(*lights, "landing_on").value_or(false);
                s.taxiLight = flag(*lights, "taxi_on").value_or(false);
                s.beaconLight = flag(*lights, "beacon_on").value_or(false);
                s.strobeLight = flag(*lights, "strobe_on").value_or(false);
                s.navLight = flag(*lights, "nav_on").value_or(false);
            }

            s.squawk = static_cast<int>(number(entry, "squawk", 2000));
            const std::string mode = text(entry, "mode");
            // FSD transponder modes: S = standby, N = mode C, Y = ident
            s.ident = mode == "Y" || mode == "y";
            s.modeC = s.ident || mode == "N" || mode == "n" || mode.empty();
            return s;
        }
    } // namespace

    std::optional<std::string> Reassembler::feed(const std::string &packet)
    {
        json header = json::parse(packet, nullptr, false);
        if (header.is_discarded() || !header.is_object()) { return std::nullopt; }
        if (number(header, "v", -1) != kProtocolVersion) { return std::nullopt; }

        const int sequence = static_cast<int>(number(header, "seq", 0)) & 0xFFFF;
        if (!m_sequence || sequence != *m_sequence)
        {
            // 16-bit wrap: only a sequence that moves forward starts a new
            // frame; late fragments of an older one are dropped.
            if (m_sequence)
            {
                const int delta = (sequence - *m_sequence) & 0xFFFF;
                if (delta == 0 || delta > 0x8000) { return std::nullopt; }
            }
            m_sequence = sequence;
            m_parts.clear();
            m_total = std::max(1, static_cast<int>(number(header, "total", 1)));
        }

        const int part = static_cast<int>(number(header, "part", -1));
        if (part < 0 || part >= m_total) { return std::nullopt; }
        const std::string data = text(header, "data");
        m_parts[part] = base64Decode(data);
        if (static_cast<int>(m_parts.size()) < m_total) { return std::nullopt; }

        std::string body;
        for (int i = 0; i < m_total; ++i) { body += m_parts[i]; }
        m_parts.clear();
        return body;
    }

    std::optional<Frame> parseFrame(const std::string &body)
    {
        json message = json::parse(body, nullptr, false);
        if (message.is_discarded() || !message.is_object()) { return std::nullopt; }
        if (text(message, "type") != "traffic") { return std::nullopt; }

        Frame frame;
        auto aircraft = message.find("aircraft");
        if (aircraft != message.end() && aircraft->is_array())
        {
            for (const auto &entry : *aircraft)
            {
                if (!entry.is_object()) { continue; }
                PlaneState state = planeFrom(entry);
                if (!state.callsign.empty()) { frame.planes.push_back(std::move(state)); }
            }
        }

        auto csl = message.find("csl");
        if (csl != message.end())
        {
            if (csl->is_string() && !csl->get<std::string>().empty()) { frame.cslPaths.push_back(csl->get<std::string>()); }
            else if (csl->is_array())
            {
                for (const auto &path : *csl)
                {
                    if (path.is_string() && !path.get<std::string>().empty()) { frame.cslPaths.push_back(path.get<std::string>()); }
                }
            }
        }
        frame.labels = flag(message, "labels");
        frame.defaultIcao = text(message, "default_icao");
        return frame;
    }

    CBridge::CBridge() : m_socket(kInvalidSocket)
    {
#ifdef _WIN32
        WSADATA data {};
        WSAStartup(MAKEWORD(2, 2), &data);
        SOCKET s = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
        if (s == INVALID_SOCKET) { return; }
        m_socket = static_cast<uintptr_t>(s);
#else
        m_socket = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
        if (m_socket < 0)
        {
            m_socket = kInvalidSocket;
            return;
        }
#endif
        sockaddr_in address {};
        address.sin_family = AF_INET;
        address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        address.sin_port = htons(kPluginPort);
#ifdef _WIN32
        const bool bound = bind(static_cast<SOCKET>(m_socket), reinterpret_cast<sockaddr *>(&address), sizeof(address)) == 0;
#else
        const bool bound = bind(m_socket, reinterpret_cast<sockaddr *>(&address), sizeof(address)) == 0;
#endif
        if (!bound)
        {
            ERROR_LOG("could not bind 127.0.0.1:" + std::to_string(kPluginPort) +
                      " - is the old Python plugin (PI_XpcTraffic.py) still installed?");
            close();
            return;
        }
#ifdef _WIN32
        u_long nonblocking = 1;
        ioctlsocket(static_cast<SOCKET>(m_socket), FIONBIO, &nonblocking);
#else
        fcntl(m_socket, F_SETFL, O_NONBLOCK);
#endif
        INFO_LOG("listening for the XPC client on 127.0.0.1:" + std::to_string(kPluginPort));
    }

    CBridge::~CBridge()
    {
        close();
#ifdef _WIN32
        WSACleanup();
#endif
    }

    bool CBridge::isOpen() const { return m_socket != kInvalidSocket; }

    void CBridge::close()
    {
        if (m_socket == kInvalidSocket) { return; }
#ifdef _WIN32
        closesocket(static_cast<SOCKET>(m_socket));
#else
        ::close(m_socket);
#endif
        m_socket = kInvalidSocket;
    }

    std::optional<Frame> CBridge::poll()
    {
        std::optional<Frame> newest;
        if (!isOpen()) { return newest; }
        static thread_local std::vector<char> buffer(65536);
        for (int i = 0; i < 1024; ++i)
        {
#ifdef _WIN32
            const int count = recvfrom(static_cast<SOCKET>(m_socket), buffer.data(), static_cast<int>(buffer.size()), 0, nullptr, nullptr);
#else
            const auto count = recvfrom(m_socket, buffer.data(), buffer.size(), 0, nullptr, nullptr);
#endif
            // On Windows an ICMP port-unreachable surfaces as WSAECONNRESET
            // on the next read; it is not a reason to stop reading.
            if (count < 0)
            {
#ifdef _WIN32
                if (WSAGetLastError() == WSAECONNRESET) { continue; }
#endif
                break;
            }
            if (count == 0) { continue; }
            auto body = m_reassembler.feed(std::string(buffer.data(), static_cast<size_t>(count)));
            if (!body) { continue; }
            auto frame = parseFrame(*body);
            if (frame) { newest = std::move(frame); }
        }
        return newest;
    }
} // namespace XpcNative
