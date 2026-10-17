package com.droiddeck.launcher.ui

import android.app.Application
import androidx.compose.ui.test.onAllNodesWithContentDescription
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextReplacement
import com.droiddeck.launcher.R
import com.droiddeck.launcher.artifact.RepositorySources
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], application = Application::class)
class RepositorySourcesEditorTest {
    @get:Rule val compose = createRobolectricComposeRule()
    private val context get() = RuntimeEnvironment.getApplication()
    private val builtIns = listOf("Droid-Deck/DroidDeck")
    private val repository = "droid-deck/droiddeck"

    private fun open() {
        compose.onNodeWithContentDescription(context.getString(R.string.sources_component_title)).performClick()
        compose.waitUntil(5_000) {
            compose.onAllNodesWithContentDescription(context.getString(R.string.sources_hide, repository))
                .fetchSemanticsNodes().isNotEmpty()
        }
    }

    @Test fun hidingIsDraftUntilSaveAndBuiltInCannotBeRemoved() {
        val scope = "editor/hide"
        var saves = 0
        compose.setContent {
            DroidDeckTheme { RepositorySourcesButton(scope, builtIns, onSaved = { saves++ }) }
        }
        open()
        compose.onNodeWithContentDescription(context.getString(R.string.sources_remove, repository)).assertDoesNotExist()
        compose.onNodeWithContentDescription(context.getString(R.string.sources_hide, repository)).performClick()
        assertFalse(RepositorySources.list(context, scope, builtIns).single().hidden)
        compose.onNodeWithText(context.getString(R.string.sources_save)).performClick()
        compose.waitUntil(5_000) { saves == 1 }
        assertTrue(RepositorySources.list(context, scope, builtIns).single().hidden)
        assertTrue(RepositorySources.visible(context, scope, builtIns).isEmpty())
    }

    @Test fun closingDiscardsDraft() {
        val scope = "editor/discard"
        compose.setContent { DroidDeckTheme { RepositorySourcesButton(scope, builtIns, onSaved = {}) } }
        open()
        compose.onNodeWithContentDescription(context.getString(R.string.sources_hide, repository)).performClick()
        compose.onNodeWithContentDescription(context.getString(R.string.sources_component_title)).performClick()
        compose.waitForIdle()
        open()
        assertFalse(RepositorySources.list(context, scope, builtIns).single().hidden)
        compose.onNodeWithContentDescription(context.getString(R.string.sources_hide, repository)).assertExists()
    }

    @Test fun addValidatesRepositoryBeforeSaveAndCustomRowCanBeRemoved() {
        val scope = "editor/add"
        var saves = 0
        compose.setContent { DroidDeckTheme { RepositorySourcesButton(scope, builtIns, onSaved = { saves++ }) } }
        open()
        compose.onNodeWithText(context.getString(R.string.sources_add)).performClick()
        val input = compose.onNodeWithText(context.getString(R.string.sources_repository))
        input.performTextReplacement("https://example.com/owner/repo")
        compose.onNodeWithText(context.getString(R.string.sources_save)).performClick()
        compose.onNodeWithText(context.getString(R.string.sources_invalid_repository)).assertExists()
        assertEquals(0, saves)
        input.performTextReplacement("https://github.com/Wallentx/DroidDeck")
        compose.onNodeWithText(context.getString(R.string.sources_save)).performClick()
        compose.waitUntil(5_000) { saves == 1 }
        assertEquals(listOf(repository, "wallentx/droiddeck"), RepositorySources.list(context, scope, builtIns).map { it.repository })
        open()
        compose.onNodeWithContentDescription(context.getString(R.string.sources_remove, "wallentx/droiddeck")).performClick()
        assertEquals(2, RepositorySources.list(context, scope, builtIns).size)
        compose.onNodeWithText(context.getString(R.string.sources_save)).performClick()
        compose.waitUntil(5_000) { saves == 2 }
        assertEquals(listOf(repository), RepositorySources.list(context, scope, builtIns).map { it.repository })
    }
}
