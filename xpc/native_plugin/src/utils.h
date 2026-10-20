// SPDX-FileCopyrightText: Copyright (C) 2013 swift Project Community / Contributors
// SPDX-FileCopyrightText: Copyright (C) 2026 XPC for CAN contributors
// SPDX-License-Identifier: GPL-3.0-or-later
//
// Logger from swift's xswiftbus (src/xswiftbus/utils.h): everything goes to
// X-Plane's Log.txt with an "XPC for CAN: " prefix.

#ifndef XPC_NATIVE_UTILS_H
#define XPC_NATIVE_UTILS_H

#include <string>

namespace XpcNative
{
    class Logger
    {
    public:
        enum MsgType
        {
            DebugMsg,
            WarningMsg,
            ErrorMsg,
            InfoMsg
        };

        Logger() = delete;

        //! Print message to X-Plane log
        static void print(MsgType type, const std::string &message);

        //! Debug lines are dropped unless enabled
        static bool s_debug;
    };
} // namespace XpcNative

#define DEBUG_LOG(msg) (XpcNative::Logger::s_debug ? XpcNative::Logger::print(XpcNative::Logger::DebugMsg, (msg)) : static_cast<void>(0))
#define INFO_LOG(msg) (XpcNative::Logger::print(XpcNative::Logger::InfoMsg, (msg)))
#define WARNING_LOG(msg) (XpcNative::Logger::print(XpcNative::Logger::WarningMsg, (msg)))
#define ERROR_LOG(msg) (XpcNative::Logger::print(XpcNative::Logger::ErrorMsg, (msg)))

#endif // XPC_NATIVE_UTILS_H
