# XPC for CAN native X-Plane plugin.
#
# The renderer is swift's xplanemp2 (xplanemp2/, MIT), built into the plugin
# exactly the way swift builds it into xswiftbus (src/xswiftbus/CMakeLists.txt).
cmake_minimum_required(VERSION 3.20)
project(xpc_for_can_native LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
# No dependency on the VC++ redistributable inside X-Plane's process
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")

# Windows only: XPC for CAN ships for Windows X-Plane installs.
if(NOT WIN32)
    message(FATAL_ERROR "The XPC for CAN plugin is built for Windows (win.xpl) only")
endif()

if(NOT XPLANE_SDK)
    message(FATAL_ERROR "Set XPLANE_SDK to the X-Plane SDK root")
endif()

add_library(XPC_for_CAN SHARED
    src/bridge.cpp
    src/bridge.h
    src/plugin.cpp
    src/traffic.cpp
    src/traffic.h
    src/utils.cpp
    src/utils.h

    xplanemp2/src/CSL.cpp
    xplanemp2/src/CSLLibrary.cpp
    xplanemp2/src/CullInfo.cpp
    xplanemp2/src/PlanesHandoff.c
    xplanemp2/src/PlaneType.cpp
    xplanemp2/src/Renderer.cpp
    xplanemp2/src/TCASOverride.cpp
    xplanemp2/src/XPMPMultiplayer.cpp
    xplanemp2/src/XPMPMultiplayerVars.cpp
    xplanemp2/src/XPMPPlane.cpp
    xplanemp2/src/XStringUtils.cpp
    xplanemp2/src/XUtils.cpp
    xplanemp2/src/obj8/Obj8Attachment.cpp
    xplanemp2/src/obj8/Obj8CSL.cpp
    xplanemp2/src/obj8/Obj8InstanceData.cpp
)

target_include_directories(XPC_for_CAN PRIVATE
    src
    third_party
    xplanemp2/include
    xplanemp2/src
    "${XPLANE_SDK}/CHeaders/XPLM"
    "${XPLANE_SDK}/CHeaders/Widgets"
)

target_compile_definitions(XPC_for_CAN PRIVATE
    XPLM200=1 XPLM210=1 XPLM300=1 XPLM_DEPRECATED=1
    XPMP_CLIENT_NAME="XPC_for_CAN"
    XPMP_CLIENT_LONGNAME="XPC for CAN"
)

target_compile_definitions(XPC_for_CAN PRIVATE IBM=1 _USE_MATH_DEFINES=1 _CRT_SECURE_NO_WARNINGS NOMINMAX)
target_link_libraries(XPC_for_CAN PRIVATE
    "${XPLANE_SDK}/Libraries/Win/XPLM_64.lib"
    ws2_32
)
set(PLUGIN_OUTPUT win)

if(MSVC)
    target_compile_options(XPC_for_CAN PRIVATE /utf-8 /W3)
endif()

# X-Plane loads <plugin>/64/win.xpl
set_target_properties(XPC_for_CAN PROPERTIES
    PREFIX ""
    SUFFIX ".xpl"
    OUTPUT_NAME "${PLUGIN_OUTPUT}"
)

# A ready-to-copy plugin folder: package/XPC_for_CAN/{64/win.xpl, LegacyData/}
set(PACKAGE_DIR "${CMAKE_CURRENT_BINARY_DIR}/package/XPC_for_CAN")
add_custom_command(TARGET XPC_for_CAN POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E make_directory "${PACKAGE_DIR}/64"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "$<TARGET_FILE:XPC_for_CAN>" "${PACKAGE_DIR}/64/${PLUGIN_OUTPUT}.xpl"
    COMMAND ${CMAKE_COMMAND} -E copy_directory
        "${CMAKE_CURRENT_SOURCE_DIR}/LegacyData" "${PACKAGE_DIR}/LegacyData"
)
