package com.droiddeck.launcher.gpu

import org.junit.Assert.assertEquals
import org.junit.Test

class DeviceInfoTest {
    @Test fun builtinLabelsDistinguishPowerVrFromTurnipAndGenericSystemVulkan() {
        assertEquals("System PowerVR", DeviceInfo.builtinDriverLabel(powerVr = true, runtime = true))
        assertEquals("System PowerVR", DeviceInfo.builtinDriverLabel(powerVr = true, runtime = false))
        assertEquals("the runtime's own Turnip", DeviceInfo.builtinDriverLabel(powerVr = false, runtime = true))
        assertEquals("System Vulkan", DeviceInfo.builtinDriverLabel(powerVr = false, runtime = false))
    }

    @Test fun maliLabelsNameTheSystemDriverForBothRuntimeAndDisplay() {
        assertEquals("System Mali", DeviceInfo.builtinDriverLabel(powerVr = false, runtime = true, mali = true))
        assertEquals("System Mali", DeviceInfo.builtinDriverLabel(powerVr = false, runtime = false, mali = true))
    }
}
