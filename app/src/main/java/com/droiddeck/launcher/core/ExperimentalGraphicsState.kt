package com.droiddeck.launcher.core

/** Selected launch policy, not a claim about the graphics of an already running game. */
data class ExperimentalGraphicsState(
    val selected: Boolean = false,
    val selectedVersion: String? = null,
    val availableVersion: String? = null,
    val selectedInstalled: Boolean = false,
    val compatible: Boolean = true,
    val checking: Boolean = false,
    val selectedDxvkRevision: String? = null,
    val updateCheckFailed: Boolean = false,
) {
    val controlsDxvk: Boolean get() = selected
    val selectedRevision: String? get() = selectedVersion?.take(8)
    val needsRepair: Boolean get() = selected && !checking && !selectedInstalled
    val updateAvailable: Boolean
        get() = selected && selectedInstalled && selectedVersion != null &&
            availableVersion != null && selectedVersion != availableVersion

    fun controlsComponent(component: String): Boolean = selected && component == "dxvk"
    fun allowsRuntimeDriver(id: String): Boolean = !selected || id.isEmpty()
    fun allowsDisplayDriver(changesRuntime: Boolean): Boolean = !selected || !changesRuntime
    fun controlsEnvironmentKey(name: String): Boolean = selected && name in MANAGED_ENVIRONMENT_KEYS

    companion object {
        /** Keys applied after the user's shared and per-game settings at a Proton game launch. */
        val MANAGED_ENVIRONMENT_KEYS: Set<String> = setOf(
            "DROIDDECK_PROTON_WRAPPER", "HYBRIS_BC_TEXTURES", "VK_LAYER_PATH",
            "VK_INSTANCE_LAYERS", "ENABLE_GAMESCOPE_WSI", "VK_LOADER_LAYERS_DISABLE",
            "DISABLE_WSI_LAYER", "PROTON_USE_WINED3D",
            "DISABLE_VK_LAYER_VALVE_steam_fossilize_1", "ENABLE_VK_LAYER_VALVE_steam_fossilize_1",
        )
    }
}
