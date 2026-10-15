package com.droiddeck.launcher.session

import android.content.Context
import android.util.AtomicFile
import com.droiddeck.launcher.core.GameEnvironment
import com.droiddeck.launcher.runtime.LinuxRuntime
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
class GameEnvironmentStoreTest {
    private lateinit var context: Context
    private lateinit var guest: File

    @Before fun setUp() {
        context = RuntimeEnvironment.getApplication()
        context.getSharedPreferences("session", Context.MODE_PRIVATE).edit()
            .remove("logs")
            .remove("gameDebugLogsEnabled")
            .commit()
        guest = File(LinuxRuntime.rootDir(context), "root/.config/droiddeck/game-environment.json")
        guest.parentFile?.let { if (it.isFile) it.delete(); it.mkdirs() }
        File(context.filesDir, "game-environment.json").delete()
        AtomicFile(File(context.filesDir, "power-vr-graphics.json")).delete()
    }

    @After fun tearDown() {
        context.getSharedPreferences("session", Context.MODE_PRIVATE).edit()
            .remove("logs")
            .remove("gameDebugLogsEnabled")
            .commit()
    }

    @Test fun gameDebugLogsAreOffByDefault() {
        assertFalse(SessionPrefs.gameDebugLogsEnabled(context))

        GameEnvironmentStore.save(context, GameEnvironment.Config())

        assertFalse(JSONObject(guest.readText()).getBoolean(GameEnvironmentStore.GAME_DEBUG_LOGS))
    }

    @Test fun gameDebugLogsRequireSessionLogsAndPreserveCustomDiagnostics() {
        val config = GameEnvironment.Config(shared = mapOf("PROTON_LOG" to "0", "WINEDEBUG" to "+warn"))
        GameEnvironmentStore.save(context, config)

        SessionPrefs.setGameDebugLogsEnabled(context, true)
        var published = JSONObject(guest.readText())
        assertTrue(published.getBoolean(GameEnvironmentStore.GAME_DEBUG_LOGS))
        assertEquals("0", published.getJSONObject("shared").getString("PROTON_LOG"))
        assertEquals("+warn", published.getJSONObject("shared").getString("WINEDEBUG"))
        assertEquals(config, GameEnvironmentStore.read(context))

        SessionPrefs.setLogsEnabled(context, false)
        published = JSONObject(guest.readText())
        assertFalse(published.getBoolean(GameEnvironmentStore.GAME_DEBUG_LOGS))
        assertTrue(SessionPrefs.gameDebugLogsEnabled(context))

        SessionPrefs.setLogsEnabled(context, true)
        assertTrue(JSONObject(guest.readText()).getBoolean(GameEnvironmentStore.GAME_DEBUG_LOGS))
    }

    @Test fun gameDebugLogsWaitForARetainedSessionFolder() {
        assertNull(SessionPaths.current())
        assertTrue(SessionPaths.currentSessionLogsRetained(context))
        SessionPrefs.setLogsEnabled(context, false)
        val cacheSession = SessionPaths.beginOrCurrent(context, "game-debug-logs")
        try {
            assertFalse(SessionPaths.currentSessionLogsRetained(context))

            SessionPrefs.setLogsEnabled(context, true)
            SessionPrefs.setGameDebugLogsEnabled(context, true)
            assertTrue(SessionPrefs.gameDebugLogsEnabled(context))
            assertFalse(JSONObject(guest.readText()).getBoolean(GameEnvironmentStore.GAME_DEBUG_LOGS))

            assertEquals(cacheSession, SessionPaths.take())
            SessionPaths.release(context, cacheSession)
            val retainedSession = SessionPaths.beginOrCurrent(context, "game-debug-logs")
            assertTrue(SessionPaths.currentSessionLogsRetained(context))

            GameEnvironmentStore.publish(context)
            assertTrue(JSONObject(guest.readText()).getBoolean(GameEnvironmentStore.GAME_DEBUG_LOGS))
            assertEquals(retainedSession, SessionPaths.current())
        } finally {
            SessionPaths.take()?.let { ended ->
                SessionPaths.release(context, ended)
                ended.deleteRecursively()
            }
        }
    }

    @Test fun savedSettingsAndPublishedSettingsPreserveUnsetsAndLiteralValues() {
        val config = GameEnvironment.Config(
            shared = mapOf("LITERAL" to "a=b 'quoted' $(echo test)", "EMPTY" to ""),
            games = mapOf("42" to mapOf("LITERAL" to null, "VKD3D_SHADER_MODEL" to "6_9")),
        )
        GameEnvironmentStore.save(context, config)
        assertEquals(config, GameEnvironmentStore.read(context))
        val published = GameEnvironmentStore.decode(JSONObject(guest.readText()))
        assertEquals("false", published.shared["MESA_SHADER_CACHE_DISABLE"])
        assertEquals("12_2", published.shared["VKD3D_FEATURE_LEVEL"])
        assertEquals("6_9", published.shared["VKD3D_SHADER_MODEL"])
        assertEquals(config.games, published.games)
        assertEquals(config.shared["LITERAL"], published.shared["LITERAL"])
        assertEquals("", published.shared["EMPTY"])
    }

    @Test fun changingFexPresetPublishesForNextGameLaunch() {
        GameEnvironmentStore.save(context, GameEnvironment.Config())
        SessionPrefs.setFexPreset(context, "EXTREME")
        assertEquals("none", JSONObject(guest.readText()).getJSONObject("shared").getString("FEX_SMCCHECKS"))
        SessionPrefs.setFexPreset(context, "")
        assertFalse(JSONObject(guest.readText()).getJSONObject("shared").has("FEX_SMCCHECKS"))
    }

    @Test fun textureFilteringPublishesDxvkOptionsBesideTheProfiles() {
        GameEnvironmentStore.save(context, GameEnvironment.Config(shared = mapOf("DXVK_CONFIG" to "dxvk.tearFree = True")))
        assertFalse(JSONObject(guest.readText()).has(GameEnvironmentStore.DXVK_CONFIG))
        SessionState.upscaleRatio = 1.5f
        SessionPrefs.setTextureAnisotropy(context, 16)
        SessionPrefs.setTextureLodBias(context, com.droiddeck.launcher.core.TextureFiltering.LOD_BIAS_AUTO)
        val json = JSONObject(guest.readText())
        assertEquals(
            "d3d9.samplerAnisotropy = 16; d3d11.samplerAnisotropy = 16; d3d9.samplerLodBias = -0.58; d3d11.samplerLodBias = -0.58",
            json.getString(GameEnvironmentStore.DXVK_CONFIG),
        )
        // The user's own entry is untouched; the launcher appends the options at launch.
        assertEquals("dxvk.tearFree = True", json.getJSONObject("shared").getString("DXVK_CONFIG"))
        assertEquals(GameEnvironment.Config(shared = mapOf("DXVK_CONFIG" to "dxvk.tearFree = True")), GameEnvironmentStore.read(context))
        SessionPrefs.setTextureAnisotropy(context, 0)
        SessionPrefs.setTextureLodBias(context, "0")
        assertFalse(JSONObject(guest.readText()).has(GameEnvironmentStore.DXVK_CONFIG))
        SessionState.upscaleRatio = 0f
    }

    @Test fun unchosenFexPresetPublishesPerformanceTso() {
        GameEnvironmentStore.save(context, GameEnvironment.Config())
        val shared = JSONObject(guest.readText()).getJSONObject("shared")
        assertEquals("1", shared.getString("FEX_TSOENABLED"))
        assertEquals("0", shared.getString("FEX_HALFBARRIERTSOENABLED"))
        assertEquals("1", shared.getString("FEX_X87REDUCEDPRECISION"))
        assertEquals("1", shared.getString("FEX_MULTIBLOCK"))
    }

    @Test fun invalidDataCannotReplaceSavedConfiguration() {
        val original = GameEnvironment.Config(shared = mapOf("CUSTOM" to "ok"))
        GameEnvironmentStore.save(context, original)
        assertTrue(runCatching { GameEnvironmentStore.save(context, GameEnvironment.Config(shared = mapOf("BAD=NAME" to "x"))) }.isFailure)
        assertEquals(original, GameEnvironmentStore.read(context))
    }

    @Test fun customComponentVariablesRemainEditable() {
        val config = GameEnvironment.Config(shared = mapOf("DXVK_ASYNC" to "1"))
        GameEnvironmentStore.save(context, config)
        assertEquals(config, GameEnvironmentStore.read(context))
    }

    @Test fun standardGraphicsChoicePersistsAndRepublishesWithoutChangingRawEnvironment() {
        val config = GameEnvironment.Config(
            shared = mapOf("DISABLE_WSI_LAYER" to "user", "PROTON_USE_WINED3D" to null),
            games = mapOf("42" to mapOf("DROIDDECK_PROTON_WRAPPER" to "/user/wrapper")),
        )
        GameEnvironmentStore.save(context, config)

        PowerVrGraphicsProfile.useStandard(context)

        assertEquals(PowerVrGraphicsProfile.Choice(PowerVrGraphicsProfile.Mode.STANDARD), PowerVrGraphicsProfile.choice(context))
        assertEquals(config, GameEnvironmentStore.read(context))
        val published = JSONObject(guest.readText())
        assertFalse(published.has(GameEnvironmentStore.GRAPHICS_PROFILE))
        assertEquals("user", published.getJSONObject("shared").getString("DISABLE_WSI_LAYER"))
        assertTrue(published.getJSONObject("shared").isNull("PROTON_USE_WINED3D"))
        assertEquals("/user/wrapper", published.getJSONObject("games").getJSONObject("42").getString("DROIDDECK_PROTON_WRAPPER"))
    }

    @Test fun corruptGraphicsChoiceReturnsUndecided() {
        File(context.filesDir, "power-vr-graphics.json").writeText("{not json")
        assertEquals(PowerVrGraphicsProfile.Choice(PowerVrGraphicsProfile.Mode.UNDECIDED), PowerVrGraphicsProfile.choice(context))
    }

    @Test fun failedGuestRepublishRollsBackTheGraphicsChoice() {
        val version = "a".repeat(64)
        File(context.filesDir, "power-vr-graphics.json").writeText(
            JSONObject().put("format", 1).put("mode", "experimental").put("version", version).toString(),
        )
        val blocker = guest.parentFile!!
        blocker.deleteRecursively()
        blocker.parentFile!!.mkdirs()
        blocker.writeText("not a directory")
        try {
            assertTrue(runCatching { PowerVrGraphicsProfile.useStandard(context) }.isFailure)
            assertEquals(
                PowerVrGraphicsProfile.Choice(PowerVrGraphicsProfile.Mode.EXPERIMENTAL, version),
                PowerVrGraphicsProfile.choice(context),
            )
        } finally {
            blocker.delete()
            blocker.mkdirs()
        }
    }

    @Test fun canceledConditionalChoiceRollsBackItsPublishedSelection() {
        val checks = AtomicInteger()
        val committed = PowerVrGraphicsProfile.useStandardIf(context) { checks.incrementAndGet() == 1 }

        assertFalse(committed)
        assertEquals(2, checks.get())
        assertEquals(
            PowerVrGraphicsProfile.Choice(PowerVrGraphicsProfile.Mode.UNDECIDED),
            PowerVrGraphicsProfile.choice(context),
        )
        assertFalse(JSONObject(guest.readText()).has(GameEnvironmentStore.GRAPHICS_PROFILE))
    }

    @Test fun canceledOlderTransitionCannotRollbackANewerChoice() {
        val oldAtCommit = CountDownLatch(1)
        val releaseOld = CountDownLatch(1)
        val newStarted = CountDownLatch(1)
        val newDone = CountDownLatch(1)
        val checks = AtomicInteger()
        val error = AtomicReference<Throwable?>()
        val old = Thread {
            try {
                PowerVrGraphicsProfile.useStandardIf(context) {
                    if (checks.incrementAndGet() == 1) true else {
                        oldAtCommit.countDown()
                        releaseOld.await()
                        false
                    }
                }
            } catch (failure: Throwable) {
                error.compareAndSet(null, failure)
            }
        }
        val newer = Thread {
            newStarted.countDown()
            try {
                PowerVrGraphicsProfile.useStandardIf(context) { true }
            } catch (failure: Throwable) {
                error.compareAndSet(null, failure)
            } finally {
                newDone.countDown()
            }
        }

        old.start()
        try {
            assertTrue(oldAtCommit.await(2, TimeUnit.SECONDS))
            newer.start()
            assertTrue(newStarted.await(2, TimeUnit.SECONDS))
            assertFalse(newDone.await(100, TimeUnit.MILLISECONDS))
        } finally {
            releaseOld.countDown()
            old.join(2_000)
            if (newer.state != Thread.State.NEW) newer.join(2_000)
        }

        error.get()?.let { throw AssertionError(it) }
        assertFalse(old.isAlive)
        assertFalse(newer.isAlive)
        assertEquals(
            PowerVrGraphicsProfile.Choice(PowerVrGraphicsProfile.Mode.STANDARD),
            PowerVrGraphicsProfile.choice(context),
        )
    }

    @Test fun republishWaitsForRollbackBeforeResolvingARecreatedLaunch() {
        val oldAtCommit = CountDownLatch(1)
        val releaseOld = CountDownLatch(1)
        val readerStarted = CountDownLatch(1)
        val readerDone = CountDownLatch(1)
        val checks = AtomicInteger()
        val result = AtomicReference<PowerVrGraphicsProfile.Choice?>()
        val error = AtomicReference<Throwable?>()
        val old = Thread {
            try {
                PowerVrGraphicsProfile.useStandardIf(context) {
                    if (checks.incrementAndGet() == 1) true else {
                        oldAtCommit.countDown()
                        releaseOld.await()
                        false
                    }
                }
            } catch (failure: Throwable) {
                error.compareAndSet(null, failure)
            }
        }
        val reader = Thread {
            readerStarted.countDown()
            try {
                result.set(PowerVrGraphicsProfile.republishIf(context) { true })
            } catch (failure: Throwable) {
                error.compareAndSet(null, failure)
            } finally {
                readerDone.countDown()
            }
        }

        old.start()
        try {
            assertTrue(oldAtCommit.await(2, TimeUnit.SECONDS))
            assertEquals(PowerVrGraphicsProfile.Mode.STANDARD, PowerVrGraphicsProfile.choice(context).mode)
            reader.start()
            assertTrue(readerStarted.await(2, TimeUnit.SECONDS))
            assertFalse(readerDone.await(100, TimeUnit.MILLISECONDS))
        } finally {
            releaseOld.countDown()
            old.join(2_000)
            if (reader.state != Thread.State.NEW) reader.join(2_000)
        }

        error.get()?.let { throw AssertionError(it) }
        assertFalse(old.isAlive)
        assertFalse(reader.isAlive)
        assertEquals(PowerVrGraphicsProfile.Choice(PowerVrGraphicsProfile.Mode.UNDECIDED), result.get())
        assertFalse(JSONObject(guest.readText()).has(GameEnvironmentStore.GRAPHICS_PROFILE))
    }
}
