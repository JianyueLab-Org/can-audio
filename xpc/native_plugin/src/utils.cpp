// SPDX-FileCopyrightText: Copyright (C) 2016 swift Project Community / Contributors
// SPDX-FileCopyrightText: Copyright (C) 2026 XPC for CAN contributors
// SPDX-License-Identifier: GPL-3.0-or-later

#include "utils.h"

#include <XPLMUtilities.h>

namespace XpcNative
{
    bool Logger::s_debug = false;

    void Logger::print(MsgType type, const std::string &message)
    {
        std::string buffer = "XPC for CAN: ";
        switch (type)
        {
        case DebugMsg: buffer += "debug: "; break;
        case WarningMsg: buffer += "warning: "; break;
        case ErrorMsg: buffer += "error: "; break;
        case InfoMsg: break;
        }
        buffer += message;
        buffer += "\n";
        XPLMDebugString(buffer.c_str());
    }
} // namespace XpcNative
