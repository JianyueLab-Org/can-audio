// SPDX-FileCopyrightText: Copyright (C) 2026 XPC for CAN contributors
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The local UDP link from the XPC client (xpc/bridge.py). This is what takes
// the place of swift's D-Bus: one JSON frame per message, fragmented over
// seq/part/total with base64 payloads, newest frame wins.

#ifndef XPC_NATIVE_BRIDGE_H
#define XPC_NATIVE_BRIDGE_H

#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <vector>

#include "traffic.h"

//! Must equal bridge.PROTOCOL_VERSION in xpc/bridge.py. xpinstall.py reads
//! the "XPC_BRIDGE_PROTOCOL=<n>" marker built from it out of the installed
//! .xpl, so a mismatch is reported instead of traffic silently not appearing.
#define XPC_BRIDGE_PROTOCOL 3
#define XPC_STRINGIFY2(x) #x
#define XPC_STRINGIFY(x) XPC_STRINGIFY2(x)
#define XPC_BRIDGE_PROTOCOL_MARKER "XPC_BRIDGE_PROTOCOL=" XPC_STRINGIFY(XPC_BRIDGE_PROTOCOL)

namespace XpcNative
{
    //! Must equal bridge.PROTOCOL_VERSION in xpc/bridge.py
    constexpr int kProtocolVersion = XPC_BRIDGE_PROTOCOL;
    constexpr int kPluginPort = 49900;

    //! A decoded frame from the client
    struct Frame
    {
        std::vector<PlaneState> planes;
        std::vector<std::string> cslPaths;
        std::optional<bool> labels;
        std::string defaultIcao;
    };

    //! Port of bridge.Reassembler
    class Reassembler
    {
    public:
        //! Feed one datagram; returns the reassembled body once complete
        std::optional<std::string> feed(const std::string &packet);

    private:
        std::optional<int> m_sequence;
        std::map<int, std::string> m_parts;
        int m_total = 0;
    };

    //! Parse a reassembled body. Returns nothing for a non-traffic or broken frame.
    std::optional<Frame> parseFrame(const std::string &body);

    class CBridge
    {
    public:
        CBridge();
        ~CBridge();

        bool isOpen() const;

        //! Drain the socket; returns the newest complete frame, if any arrived
        std::optional<Frame> poll();

    private:
        void close();

#ifdef _WIN32
        uintptr_t m_socket;
#else
        int m_socket;
#endif
        Reassembler m_reassembler;
    };
} // namespace XpcNative

#endif // XPC_NATIVE_BRIDGE_H
