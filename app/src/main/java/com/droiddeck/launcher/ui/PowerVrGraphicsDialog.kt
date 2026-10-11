package com.droiddeck.launcher.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.hoverable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.selection.selectableGroup
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.RadioButton
import androidx.compose.material3.RadioButtonDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.droiddeck.launcher.R
import com.droiddeck.launcher.session.PowerVrGraphicsProfile

internal enum class PowerVrGraphicsDialogPurpose { FIRST_LAUNCH, REPAIR, SETTINGS }

internal data class PowerVrGraphicsUiState(
    val purpose: PowerVrGraphicsDialogPurpose,
    val mode: PowerVrGraphicsProfile.Mode = PowerVrGraphicsProfile.Mode.UNDECIDED,
    val selectedVersion: String? = null,
    val availableVersion: String? = null,
    val selectedInstalled: Boolean = false,
    val availableInstalled: Boolean = false,
    val compatible: Boolean = true,
    val loading: Boolean = false,
    val working: Boolean = false,
    val progress: Int = -1,
    val failed: Boolean = false,
    val failureMessage: String? = null,
) {
    val updateAvailable: Boolean
        get() = mode == PowerVrGraphicsProfile.Mode.EXPERIMENTAL && selectedInstalled &&
            selectedVersion != null && availableVersion != null && selectedVersion != availableVersion

    val needsRepair: Boolean
        get() = mode == PowerVrGraphicsProfile.Mode.EXPERIMENTAL && !selectedInstalled
}

/** Global Steam graphics choice shown only on PowerVR devices. */
@Composable
internal fun PowerVrGraphicsDialog(
    state: PowerVrGraphicsUiState,
    onStandard: () -> Unit,
    onExperimental: () -> Unit,
    onCancel: () -> Unit,
    onDismiss: () -> Unit,
) {
    val shown = rememberShown(onDismiss)
    val close = { onCancel(); shown.targetState = false }
    val selectedStandard = state.mode == PowerVrGraphicsProfile.Mode.STANDARD
    val selectedExperimental = state.mode == PowerVrGraphicsProfile.Mode.EXPERIMENTAL
    val idle = !state.loading && !state.working
    val experimentalAction = when {
        state.failed -> R.string.power_vr_graphics_retry
        state.needsRepair -> R.string.power_vr_graphics_repair
        state.updateAvailable -> R.string.power_vr_graphics_update
        selectedExperimental -> null
        state.availableInstalled -> R.string.power_vr_graphics_use_experimental
        else -> R.string.power_vr_graphics_download_experimental
    }

    AppDialog(shown, close, "powerVrGraphics", wide = false, maxWidth = 640.dp) {
        DialogHeader(
            stringResource(R.string.power_vr_graphics_eyebrow),
            stringResource(
                if (state.purpose == PowerVrGraphicsDialogPurpose.FIRST_LAUNCH) {
                    R.string.power_vr_graphics_choose_title
                } else {
                    R.string.power_vr_graphics_title
                },
            ),
        )
        Text(
            stringResource(R.string.power_vr_graphics_intro),
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            fontSize = 14.sp,
            lineHeight = 20.sp,
        )

        Column(
            verticalArrangement = Arrangement.spacedBy(8.dp),
            modifier = Modifier.fillMaxWidth().selectableGroup(),
        ) {
            PowerVrChoiceRow(
                title = stringResource(R.string.power_vr_graphics_standard),
                detail = stringResource(R.string.power_vr_graphics_standard_detail),
                selected = selectedStandard,
                action = if (!selectedStandard || state.failed) stringResource(R.string.power_vr_graphics_use_standard) else null,
                actionEnabled = idle && (!selectedStandard || state.failed),
                onClick = onStandard,
            )
            PowerVrChoiceRow(
                title = stringResource(R.string.power_vr_graphics_experimental),
                detail = stringResource(R.string.power_vr_graphics_experimental_detail),
                selected = selectedExperimental,
                action = experimentalAction?.let { stringResource(it) },
                available = state.compatible,
                actionEnabled = idle && state.compatible &&
                    (!selectedExperimental || state.needsRepair || state.updateAvailable || state.failed),
                onClick = onExperimental,
            )
        }

        val status = when {
            state.loading -> stringResource(R.string.power_vr_graphics_checking)
            state.working -> stringResource(R.string.power_vr_graphics_applying)
            state.failed -> state.failureMessage?.takeIf { it.isNotBlank() }
                ?: stringResource(R.string.power_vr_graphics_failed)
            !state.compatible -> stringResource(R.string.power_vr_graphics_custom_driver)
            state.needsRepair -> stringResource(R.string.power_vr_graphics_repair_needed)
            state.updateAvailable -> stringResource(R.string.power_vr_graphics_update_available)
            selectedExperimental && state.selectedInstalled -> stringResource(R.string.power_vr_graphics_ready)
            state.availableInstalled -> stringResource(R.string.power_vr_graphics_downloaded)
            else -> stringResource(R.string.power_vr_graphics_shared_download)
        }
        Small(status, error = idle && (state.failed || state.needsRepair || !state.compatible))

        if (state.working || state.loading) {
            if (state.progress >= 0) {
                LinearProgressIndicator(
                    progress = { state.progress.coerceIn(0, 100) / 100f },
                    modifier = Modifier.fillMaxWidth(),
                )
            } else {
                LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
            }
        }

        Small(stringResource(R.string.power_vr_graphics_next_game))
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
            SecondaryButton(stringResource(R.string.common_cancel), onClick = close)
        }
    }
}

@Composable
private fun PowerVrChoiceRow(
    title: String,
    detail: String,
    selected: Boolean,
    action: String?,
    available: Boolean = true,
    actionEnabled: Boolean,
    onClick: () -> Unit,
) {
    val colors = MaterialTheme.colorScheme
    val pal = LocalPalette.current
    val source = remember { MutableInteractionSource() }
    val hot = rememberHot(source) && actionEnabled
    val shape = RoundedCornerShape(14.dp)
    Column(
        verticalArrangement = Arrangement.spacedBy(8.dp),
        modifier = Modifier.fillMaxWidth()
            .paneItem("power-vr-mode:$title")
            .heightIn(min = 56.dp)
            .clip(shape)
            .background(if (selected || hot) pal.signal.copy(alpha = 0.10f) else colors.surface)
            .glideBorder(hot, shape, pal.signal, pal.line)
            .hoverable(source)
            .selectable(
                selected = selected,
                enabled = actionEnabled,
                role = Role.RadioButton,
                interactionSource = source,
                indication = null,
                onClick = onClick,
            )
            .controllerConfirm(enabled = actionEnabled, onClick = onClick)
            .padding(horizontal = 14.dp, vertical = 12.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Column(Modifier.weight(1f)) {
                Text(
                    title,
                    color = if (available) colors.onBackground else colors.onSurfaceVariant,
                    fontSize = 15.sp,
                    fontWeight = FontWeight.SemiBold,
                )
                if (selected) Text(
                    stringResource(R.string.power_vr_graphics_selected),
                    color = pal.signal,
                    fontSize = 12.sp,
                    fontWeight = FontWeight.SemiBold,
                )
            }
            RadioButton(
                selected = selected,
                onClick = null,
                enabled = available,
                colors = RadioButtonDefaults.colors(selectedColor = pal.signal, unselectedColor = colors.onSurfaceVariant),
            )
        }
        Text(
            detail,
            color = colors.onSurfaceVariant.copy(alpha = if (available) 1f else 0.6f),
            fontSize = 13.sp,
            lineHeight = 18.sp,
        )
        if (action != null) Text(
            action,
            color = if (actionEnabled) pal.signal else colors.onSurfaceVariant,
            fontSize = 13.sp,
            fontWeight = FontWeight.SemiBold,
        )
    }
}
