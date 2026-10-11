package com.droiddeck.launcher.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ExperimentalGraphicsStateTest {
    @Test fun standardAndUndecidedProfilesLeaveAllControlsAvailable() {
        val standard = ExperimentalGraphicsState()

        assertFalse(standard.controlsDxvk)
        assertFalse(standard.controlsComponent("dxvk"))
        assertFalse(standard.controlsEnvironmentKey("VK_LAYER_PATH"))
        assertTrue(standard.allowsRuntimeDriver("custom-dxvk"))
        assertTrue(standard.allowsRuntimeDriver(""))
        assertTrue(standard.allowsDisplayDriver(changesRuntime = true))
    }

    @Test fun experimentalProfileOwnsDxvkOnly() {
        val experimental = ExperimentalGraphicsState(selected = true)

        assertTrue(experimental.controlsDxvk)
        assertTrue(experimental.controlsComponent("dxvk"))
        assertFalse(experimental.controlsComponent("vkd3d"))
        assertFalse(experimental.controlsComponent("fex"))
    }

    @Test fun experimentalProfileBlocksRuntimeChangesButPermitsDisplayOnlyAndDefaultRepair() {
        val experimental = ExperimentalGraphicsState(selected = true, compatible = false)

        assertFalse(experimental.allowsRuntimeDriver("custom-vulkan"))
        assertTrue(experimental.allowsRuntimeDriver(""))
        assertFalse(experimental.allowsDisplayDriver(changesRuntime = true))
        assertTrue(experimental.allowsDisplayDriver(changesRuntime = false))
    }

    @Test fun experimentalProfileProtectsOnlyEnvironmentKeysAppliedAtLaunch() {
        val experimental = ExperimentalGraphicsState(selected = true)
        val managed = setOf(
            "DROIDDECK_PROTON_WRAPPER", "HYBRIS_BC_TEXTURES", "VK_LAYER_PATH",
            "VK_INSTANCE_LAYERS", "ENABLE_GAMESCOPE_WSI", "VK_LOADER_LAYERS_DISABLE",
            "DISABLE_WSI_LAYER", "PROTON_USE_WINED3D",
            "DISABLE_VK_LAYER_VALVE_steam_fossilize_1", "ENABLE_VK_LAYER_VALVE_steam_fossilize_1",
        )

        assertEquals(managed, ExperimentalGraphicsState.MANAGED_ENVIRONMENT_KEYS)
        managed.forEach { assertTrue("$it should be protected", experimental.controlsEnvironmentKey(it)) }
        listOf("DXVK_HUD", "DXVK_CONFIG", "STEAM_COMPAT_DATA_PATH").forEach {
            assertFalse("$it should remain configurable", experimental.controlsEnvironmentKey(it))
        }
    }

    @Test fun selectedPackAndSourceRevisionRemainVisibleWhenANewerPackIsAvailable() {
        val state = ExperimentalGraphicsState(
            selected = true,
            selectedVersion = "20261010-old-build",
            availableVersion = "20261011-new-build",
            selectedInstalled = true,
            selectedDxvkRevision = "fork-dxvk-9f4c2a1b",
        )

        assertEquals("20261010", state.selectedRevision)
        assertEquals("fork-dxvk-9f4c2a1b", state.selectedDxvkRevision)
        assertTrue(state.updateAvailable)
        assertFalse(state.needsRepair)
    }

    @Test fun missingCheckingReadyAndUpdateStatesStayDistinct() {
        val missing = ExperimentalGraphicsState(selected = true, selectedVersion = "old")
        val checking = missing.copy(checking = true)
        val ready = missing.copy(selectedInstalled = true, availableVersion = "old")
        val update = ready.copy(availableVersion = "new")
        val failedCheckWithPack = ready.copy(updateCheckFailed = true)
        val failedCheckWithoutPack = missing.copy(updateCheckFailed = true)

        assertTrue(missing.needsRepair)
        assertFalse(missing.updateAvailable)
        assertFalse(checking.needsRepair)
        assertFalse(checking.updateAvailable)
        assertFalse(ready.needsRepair)
        assertFalse(ready.updateAvailable)
        assertFalse(update.needsRepair)
        assertTrue(update.updateAvailable)
        assertFalse(failedCheckWithPack.needsRepair)
        assertTrue(failedCheckWithoutPack.needsRepair)
        assertNull(ExperimentalGraphicsState().selectedRevision)
    }
}
