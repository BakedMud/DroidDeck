/* SPDX-License-Identifier: MIT
 * Stub-driver tests compile the actual patched placed-memory layer.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <sys/mman.h>
#include <unistd.h>
static int fail_fixed_map, fail_reserve_map, layer_closes;
static void *test_mmap(void *a, size_t n, int p, int flags, int fd, off_t o) {
    if ((fail_fixed_map && fd >= 0 && (flags & MAP_FIXED)) ||
        (fail_reserve_map && (flags & MAP_FIXED) && p == PROT_NONE)) {
        errno = ENOMEM;
        return MAP_FAILED;
    }
    return mmap(a, n, p, flags, fd, o);
}
static int test_close(int fd) {
    layer_closes++;
    return close(fd);
}
#define mmap test_mmap
#define close test_close
#include "placed_layer.c"
#undef mmap
#undef close
#include <assert.h>
#include <fcntl.h>
#include <stdatomic.h>
#include <stddef.h>
#include <sys/syscall.h>
#define memfd_create(name, flags) ((int)syscall(SYS_memfd_create, name, flags))

struct fake_dispatch {
    void *dispatch;
};
static int instance_key, device_key;
static struct fake_dispatch ih = {&instance_key}, ph = {&instance_key}, dh = {&device_key};
static VkInstance I = (VkInstance)&ih;
static VkPhysicalDevice P = (VkPhysicalDevice)&ph;
static VkDevice D = (VkDevice)&dh;
static _Atomic unsigned allocs, frees, native_maps, native_unmaps, driver_frees;
static int fail_native_map, short_fd, expect_filtered, explicit_failures, have_native_placed;
static _Atomic VkExternalMemoryHandleTypeFlags last_export;
struct native_memory {
    int fd;
    size_t size;
    void *map;
};
struct native_resource {
    int image;
    VkImageCreateFlags flags;
    VkExternalMemoryHandleTypeFlags external;
};
static void *VKAPI_PTR ca(void *u, size_t n, size_t align, VkSystemAllocationScope s) {
    (void)u;
    (void)s;
    void *p = NULL;
    if (posix_memalign(&p, align, n))
        return NULL;
    allocs++;
    return p;
}
static void VKAPI_PTR cf(void *u, void *p) {
    (void)u;
    if (p) {
        frees++;
        free(p);
    }
}
static VkAllocationCallbacks callbacks = {.pfnAllocation = ca, .pfnFree = cf};
static void VKAPI_CALL dummy(void) {}
static VkResult VKAPI_CALL ne(VkPhysicalDevice p, const char *layer, uint32_t *n,
                              VkExtensionProperties *out) {
    assert(p == P);
    if (layer)
        return VK_ERROR_LAYER_NOT_PRESENT;
    uint32_t count = have_native_placed ? 2 : 1;
    if (!out) {
        *n = count;
        return VK_SUCCESS;
    }
    uint32_t written = *n < count ? *n : count;
    if (written) {
        strcpy(out[0].extensionName, FD_EXT);
        out[0].specVersion = 1;
    }
    if (written > 1) {
        strcpy(out[1].extensionName, PLACED);
        out[1].specVersion = 1;
    }
    *n = written;
    return written < count ? VK_INCOMPLETE : VK_SUCCESS;
}
static void VKAPI_CALL np(VkPhysicalDevice p, VkPhysicalDeviceMemoryProperties *out) {
    assert(p == P);
    *out = (VkPhysicalDeviceMemoryProperties){.memoryTypeCount = 3, .memoryHeapCount = 1};
    out->memoryTypes[0].propertyFlags = VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT;
    out->memoryTypes[1].propertyFlags = HOST | COHERENT;
    out->memoryTypes[2].propertyFlags = HOST | VK_MEMORY_PROPERTY_HOST_CACHED_BIT;
}
static void VKAPI_CALL nf(VkPhysicalDevice p, VkPhysicalDeviceFeatures2 *out) {
    assert(p == P);
    out->features.robustBufferAccess = VK_TRUE;
}
static void VKAPI_CALL nprop(VkPhysicalDevice p, VkPhysicalDeviceProperties2 *out) {
    assert(p == P);
    out->properties.apiVersion = VK_API_VERSION_1_3;
}
static void VKAPI_CALL nbprops(VkPhysicalDevice p, const VkPhysicalDeviceExternalBufferInfo *in,
                               VkExternalBufferProperties *out) {
    assert(p == P && in->handleType == OPAQUE);
    out->externalMemoryProperties = (VkExternalMemoryProperties){
        .externalMemoryFeatures =
            VK_EXTERNAL_MEMORY_FEATURE_EXPORTABLE_BIT | VK_EXTERNAL_MEMORY_FEATURE_IMPORTABLE_BIT,
        .compatibleHandleTypes = OPAQUE};
}
static VkResult VKAPI_CALL niprops(VkPhysicalDevice p, const VkPhysicalDeviceImageFormatInfo2 *in,
                                   VkImageFormatProperties2 *out) {
    assert(p == P);
    const VkPhysicalDeviceExternalImageFormatInfo *e =
        (void *)find_chain(in->pNext, VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_IMAGE_FORMAT_INFO);
    if ((in->format == VK_FORMAT_B8G8R8A8_UNORM && (in->usage & VK_IMAGE_USAGE_STORAGE_BIT)) ||
        (e && (in->flags & VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT)))
        return VK_ERROR_FORMAT_NOT_SUPPORTED;
    out->imageFormatProperties = (VkImageFormatProperties){.maxExtent = {4096, 4096, 4096},
                                                           .maxMipLevels = 12,
                                                           .maxArrayLayers = 256,
                                                           .sampleCounts = VK_SAMPLE_COUNT_1_BIT,
                                                           .maxResourceSize = 1ull << 30};
    VkExternalImageFormatProperties *ep =
        (void *)find_chain(out->pNext, VK_STRUCTURE_TYPE_EXTERNAL_IMAGE_FORMAT_PROPERTIES);
    if (ep)
        ep->externalMemoryProperties = (VkExternalMemoryProperties){
            .externalMemoryFeatures = VK_EXTERNAL_MEMORY_FEATURE_EXPORTABLE_BIT |
                                      VK_EXTERNAL_MEMORY_FEATURE_IMPORTABLE_BIT,
            .compatibleHandleTypes = OPAQUE};
    return VK_SUCCESS;
}
static VkResult VKAPI_CALL nci(const VkInstanceCreateInfo *ci, const VkAllocationCallbacks *a,
                               VkInstance *out) {
    (void)ci;
    (void)a;
    *out = I;
    return VK_SUCCESS;
}
static void VKAPI_CALL ndi(VkInstance i, const VkAllocationCallbacks *a) {
    assert(i == I);
    (void)a;
}
static VkResult VKAPI_CALL ncd(VkPhysicalDevice p, const VkDeviceCreateInfo *ci,
                               const VkAllocationCallbacks *a, VkDevice *out) {
    assert(p == P);
    (void)a;
    if (expect_filtered) {
        assert(!find_chain(ci->pNext,
                           VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_MAP_MEMORY_PLACED_FEATURES_EXT));
        int fd = 0;
        for (uint32_t j = 0; j < ci->enabledExtensionCount; j++) {
            assert(strcmp(ci->ppEnabledExtensionNames[j], PLACED));
            assert(strcmp(ci->ppEnabledExtensionNames[j], MAP2));
            if (!strcmp(ci->ppEnabledExtensionNames[j], FD_EXT))
                fd = 1;
        }
        assert(fd);
    }
    *out = D;
    return VK_SUCCESS;
}
static void VKAPI_CALL ndd(VkDevice h, const VkAllocationCallbacks *a) {
    assert(h == D);
    (void)a;
}
static VkResult VKAPI_CALL nab(VkDevice h, const VkBufferCreateInfo *ci,
                               const VkAllocationCallbacks *a, VkBuffer *out) {
    assert(h == D);
    (void)a;
    struct native_resource *r = calloc(1, sizeof(*r));
    const VkExternalMemoryBufferCreateInfo *e =
        (void *)find_chain(ci->pNext, VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_BUFFER_CREATE_INFO);
    if (e)
        r->external = e->handleTypes;
    *out = (VkBuffer)r;
    return VK_SUCCESS;
}
static VkResult VKAPI_CALL nai(VkDevice h, const VkImageCreateInfo *ci,
                               const VkAllocationCallbacks *a, VkImage *out) {
    assert(h == D);
    (void)a;
    struct native_resource *r = calloc(1, sizeof(*r));
    r->image = 1;
    r->flags = ci->flags;
    const VkExternalMemoryImageCreateInfo *e =
        (void *)find_chain(ci->pNext, VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO);
    if (e)
        r->external = e->handleTypes;
    *out = (VkImage)r;
    return VK_SUCCESS;
}
static void VKAPI_CALL ndb(VkDevice h, VkBuffer r, const VkAllocationCallbacks *a) {
    assert(h == D);
    (void)a;
    free(r);
}
static void VKAPI_CALL ndimage(VkDevice h, VkImage r, const VkAllocationCallbacks *a) {
    assert(h == D);
    (void)a;
    free(r);
}
static void req(VkMemoryRequirements *out) {
    *out = (VkMemoryRequirements){.size = 4096, .alignment = 4096, .memoryTypeBits = 7};
}
static void VKAPI_CALL nbr(VkDevice h, VkBuffer b, VkMemoryRequirements *out) {
    assert(h == D && b);
    req(out);
}
static void VKAPI_CALL nir(VkDevice h, VkImage b, VkMemoryRequirements *out) {
    assert(h == D && b);
    req(out);
}
static void VKAPI_CALL nbr2(VkDevice h, const VkBufferMemoryRequirementsInfo2 *in,
                            VkMemoryRequirements2 *out) {
    nbr(h, in->buffer, &out->memoryRequirements);
}
static void VKAPI_CALL nir2(VkDevice h, const VkImageMemoryRequirementsInfo2 *in,
                            VkMemoryRequirements2 *out) {
    nir(h, in->image, &out->memoryRequirements);
}
static void VKAPI_CALL ndbr(VkDevice h, const VkDeviceBufferMemoryRequirements *in,
                            VkMemoryRequirements2 *out) {
    assert(h == D && in->pCreateInfo);
    req(&out->memoryRequirements);
}
static void VKAPI_CALL ndir(VkDevice h, const VkDeviceImageMemoryRequirements *in,
                            VkMemoryRequirements2 *out) {
    assert(h == D && in->pCreateInfo);
    req(&out->memoryRequirements);
}
static VkResult VKAPI_CALL na(VkDevice h, const VkMemoryAllocateInfo *ci,
                              const VkAllocationCallbacks *a, VkDeviceMemory *out) {
    assert(h == D);
    (void)a;
    struct native_memory *m = calloc(1, sizeof(*m));
    m->size = (size_t)ci->allocationSize;
    m->fd = memfd_create("layer-test", MFD_CLOEXEC);
    assert(m->fd >= 0);
    assert(!ftruncate(m->fd, m->size));
    const VkExportMemoryAllocateInfo *e =
        (void *)find_chain(ci->pNext, VK_STRUCTURE_TYPE_EXPORT_MEMORY_ALLOCATE_INFO);
    last_export = e ? e->handleTypes : 0;
    *out = (VkDeviceMemory)m;
    return VK_SUCCESS;
}
static void VKAPI_CALL nfree(VkDevice h, VkDeviceMemory handle, const VkAllocationCallbacks *a) {
    assert(h == D);
    (void)a;
    if (!handle)
        return;
    struct native_memory *m = (void *)handle;
    assert(!m->map);
    close(m->fd);
    free(m);
    driver_frees++;
}
static VkResult VKAPI_CALL nm(VkDevice h, VkDeviceMemory handle, VkDeviceSize offset,
                              VkDeviceSize size, VkMemoryMapFlags flags, void **out) {
    assert(h == D && !flags && !offset);
    struct native_memory *m = (void *)handle;
    assert(size == VK_WHOLE_SIZE || size == m->size);
    assert(!m->map);
    if (fail_native_map)
        return VK_ERROR_MEMORY_MAP_FAILED;
    m->map = mmap(NULL, m->size, PROT_READ | PROT_WRITE, MAP_SHARED, m->fd, 0);
    assert(m->map != MAP_FAILED);
    *out = m->map;
    native_maps++;
    return VK_SUCCESS;
}
static void VKAPI_CALL nu(VkDevice h, VkDeviceMemory handle) {
    assert(h == D);
    struct native_memory *m = (void *)handle;
    assert(m->map);
    assert(!munmap(m->map, m->size));
    m->map = NULL;
    native_unmaps++;
}
static VkResult VKAPI_CALL nfd(VkDevice h, const VkMemoryGetFdInfoKHR *in, int *out) {
    assert(h == D && in->handleType == OPAQUE);
    struct native_memory *m = (void *)in->memory;
    *out = dup(m->fd);
    assert(*out >= 0);
    if (short_fd) {
        close(*out);
        *out = memfd_create("short", MFD_CLOEXEC);
        assert(!ftruncate(*out, 1));
    }
    return VK_SUCCESS;
}
static PFN_vkVoidFunction VKAPI_CALL ngdpa(VkDevice h, const char *n) {
    assert(h == D);
#define N(name, func)                                                                              \
    if (!strcmp(n, "vk" name))                                                                     \
        return (PFN_vkVoidFunction)func;
    N("DestroyDevice", ndd)
    N("CreateBuffer", nab) N("DestroyBuffer", ndb) N("CreateImage", nai) N("DestroyImage", ndimage)
        N("GetBufferMemoryRequirements", nbr) N("GetImageMemoryRequirements", nir)
            N("GetBufferMemoryRequirements2", nbr2) N("GetBufferMemoryRequirements2KHR", nbr2)
                N("GetImageMemoryRequirements2", nir2) N("GetImageMemoryRequirements2KHR", nir2)
                    N("GetDeviceBufferMemoryRequirements", ndbr)
                        N("GetDeviceImageMemoryRequirements", ndir) N("AllocateMemory", na)
                            N("FreeMemory", nfree) N("MapMemory", nm) N("UnmapMemory", nu)
                                N("GetMemoryFdKHR", nfd)
#undef N
                                    if (map2_name(n)) return NULL;
    return (PFN_vkVoidFunction)dummy;
}
static PFN_vkVoidFunction VKAPI_CALL ngipa(VkInstance h, const char *n) {
    (void)h;
#define N(name, func)                                                                              \
    if (!strcmp(n, "vk" name))                                                                     \
        return (PFN_vkVoidFunction)func;
    N("CreateInstance", nci)
    N("DestroyInstance", ndi) N("CreateDevice", ncd) N("EnumerateDeviceExtensionProperties", ne)
        N("GetPhysicalDeviceMemoryProperties", np) N("GetPhysicalDeviceFeatures2", nf)
            N("GetPhysicalDeviceFeatures2KHR", nf) N("GetPhysicalDeviceProperties2", nprop)
                N("GetPhysicalDeviceProperties2KHR", nprop)
                    N("GetPhysicalDeviceExternalBufferProperties", nbprops)
                        N("GetPhysicalDeviceImageFormatProperties2", niprops)
#undef N
                            return ngdpa(D, n);
}
static void setup(int enabled) {
    if (enabled)
        setenv("DROIDDECK_EXPERIMENTAL_PLACED_MEMORY", "1", 1);
    else
        unsetenv("DROIDDECK_EXPERIMENTAL_PLACED_MEMORY");
    VkLayerInstanceLink il = {.pfnNextGetInstanceProcAddr = ngipa};
    VkLayerInstanceCreateInfo lc = {.sType = VK_STRUCTURE_TYPE_LOADER_INSTANCE_CREATE_INFO,
                                    .function = VK_LAYER_LINK_INFO,
                                    .u.pLayerInfo = &il};
    VkInstanceCreateInfo ci = {.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, .pNext = &lc};
    VkInstance out;
    assert(l_CreateInstance(&ci, &callbacks, &out) == VK_SUCCESS && out == I);
}
static void create_device(int enabled) {
    VkLayerDeviceLink link = {.pfnNextGetInstanceProcAddr = ngipa,
                              .pfnNextGetDeviceProcAddr = ngdpa};
    VkLayerDeviceCreateInfo lc = {.sType = VK_STRUCTURE_TYPE_LOADER_DEVICE_CREATE_INFO,
                                  .function = VK_LAYER_LINK_INFO,
                                  .u.pLayerInfo = &link};
    VkPhysicalDeviceMapMemoryPlacedFeaturesEXT pf = {
        .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_MAP_MEMORY_PLACED_FEATURES_EXT,
        .pNext = &lc,
        .memoryMapPlaced = VK_TRUE,
        .memoryUnmapReserve = VK_TRUE};
    VkPhysicalDeviceVulkan12Features prefix = {
        .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_2_FEATURES,
        .pNext = &pf,
        .timelineSemaphore = VK_TRUE};
    VkPhysicalDeviceVulkan12Features saved = prefix;
    const char *exts[] = {PLACED, MAP2};
    VkDeviceCreateInfo ci = {.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,
                             .pNext = enabled ? &prefix : (void *)&lc,
                             .enabledExtensionCount = enabled ? 2 : 0,
                             .ppEnabledExtensionNames = exts};
    expect_filtered = enabled;
    VkDevice out;
    assert(l_CreateDevice(P, &ci, &callbacks, &out) == VK_SUCCESS && out == D);
    assert(!memcmp(&saved, &prefix, sizeof(prefix)));
    assert(pf.pNext == &lc);
}
static void teardown(void) {
    l_DestroyDevice(D, &callbacks);
    l_DestroyInstance(I, &callbacks);
    assert(!devices && !instances);
    assert(allocs == frees);
}
static void test_dispatch(void) {
    setup(0);
    create_device(0);
    assert(!l_GetDeviceProcAddr(D, "vkMapMemory2KHR"));
    assert(l_GetDeviceProcAddr(D, "vkCreateBuffer") == (PFN_vkVoidFunction)nab);
    uint32_t n = 0;
    assert(l_EnumerateDeviceExtensionProperties(P, NULL, &n, NULL) == VK_SUCCESS && n == 1);
    teardown();
    setup(1);
    create_device(1);
    assert(l_GetDeviceProcAddr(D, "vkMapMemory2KHR") == l_GetDeviceProcAddr(D, "vkMapMemory2"));
    assert(l_GetInstanceProcAddr(I, "vkMapMemory2KHR"));
    assert(l_GetPhysicalDeviceProcAddr(I, "vkGetPhysicalDeviceFeatures2KHR") ==
           (PFN_vkVoidFunction)l_GetPhysicalDeviceFeatures2KHR);
    n = 0;
    assert(l_EnumerateDeviceExtensionProperties(P, NULL, &n, NULL) == VK_SUCCESS && n == 3);
    VkExtensionProperties ep[3];
    n = 1;
    assert(l_EnumerateDeviceExtensionProperties(P, NULL, &n, ep) == VK_INCOMPLETE && n == 1);
    n = 3;
    assert(l_EnumerateDeviceExtensionProperties(P, LAYER_NAME, &n, ep) == VK_SUCCESS && n == 2);
    n = 0;
    assert(l_EnumerateDeviceExtensionProperties(P, "other", &n, NULL) ==
           VK_ERROR_LAYER_NOT_PRESENT);
    VkPhysicalDeviceMapMemoryPlacedFeaturesEXT pf = {
        .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_MAP_MEMORY_PLACED_FEATURES_EXT};
    VkPhysicalDeviceFeatures2 f = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2,
                                   .pNext = &pf};
    l_GetPhysicalDeviceFeatures2KHR(P, &f);
    assert(pf.memoryMapPlaced && !pf.memoryMapRangePlaced && pf.memoryUnmapReserve &&
           f.features.robustBufferAccess);
    VkPhysicalDeviceMapMemoryPlacedPropertiesEXT pp = {
        .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_MAP_MEMORY_PLACED_PROPERTIES_EXT};
    VkPhysicalDeviceProperties2 props = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2,
                                         .pNext = &pp};
    l_GetPhysicalDeviceProperties2(P, &props);
    assert(pp.minPlacedMemoryMapAlignment == page_size());
    teardown();
    puts("PASS dispatch: disabled pass-through, core/KHR aliases, feature/property exposure, "
         "extension enumeration, immutable chains");
}
static void test_resources(void) {
    setup(1);
    create_device(1);
    VkImageCreateInfo ci = {.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO,
                            .imageType = VK_IMAGE_TYPE_2D,
                            .format = VK_FORMAT_R8G8B8A8_UNORM,
                            .extent = {64, 64, 1},
                            .mipLevels = 1,
                            .arrayLayers = 1,
                            .samples = VK_SAMPLE_COUNT_1_BIT,
                            .tiling = VK_IMAGE_TILING_OPTIMAL,
                            .usage = VK_IMAGE_USAGE_SAMPLED_BIT};
    for (int mut = 0; mut < 2; mut++) {
        ci.flags = mut ? VK_IMAGE_CREATE_MUTABLE_FORMAT_BIT : 0;
        VkImage img;
        assert(l_CreateImage(D, &ci, &callbacks, &img) == VK_SUCCESS);
        assert(((struct native_resource *)img)->external == (mut ? 0 : OPAQUE));
        VkMemoryRequirements r;
        l_GetImageMemoryRequirements(D, img, &r);
        assert(r.memoryTypeBits == (mut ? 1 : 7));
        VkImageMemoryRequirementsInfo2 ri = {
            .sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_REQUIREMENTS_INFO_2, .image = img};
        VkMemoryRequirements2 r2 = {.sType = VK_STRUCTURE_TYPE_MEMORY_REQUIREMENTS_2};
        l_GetImageMemoryRequirements2(D, &ri, &r2);
        assert(r2.memoryRequirements.memoryTypeBits == r.memoryTypeBits);
        VkDeviceImageMemoryRequirements di = {
            .sType = VK_STRUCTURE_TYPE_DEVICE_IMAGE_MEMORY_REQUIREMENTS, .pCreateInfo = &ci};
        l_GetDeviceImageMemoryRequirements(D, &di, &r2);
        assert(r2.memoryRequirements.memoryTypeBits == r.memoryTypeBits);
        l_DestroyImage(D, img, &callbacks);
    }
    VkExternalMemoryImageCreateInfo ext = {
        .sType = VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO,
        .handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT};
    ci.pNext = &ext;
    VkImage img;
    assert(l_CreateImage(D, &ci, &callbacks, &img) == VK_ERROR_INVALID_EXTERNAL_HANDLE);
    assert(ext.handleTypes == VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT);
    explicit_failures++;
    VkBufferCreateInfo bc = {.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
                             .size = 4096,
                             .usage = VK_BUFFER_USAGE_TRANSFER_SRC_BIT};
    VkBuffer b;
    assert(l_CreateBuffer(D, &bc, &callbacks, &b) == VK_SUCCESS &&
           ((struct native_resource *)b)->external == OPAQUE);
    l_DestroyBuffer(D, b, &callbacks);
    teardown();
    puts("PASS resources: native mutable-image fallback, all requirements paths, OPAQUE create "
         "parity, explicit unsupported contract rejection");
}
static VkDeviceMemory allocate(void) {
    VkMemoryAllocateInfo ci = {.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
                               .allocationSize = 4096,
                               .memoryTypeIndex = 1};
    VkDeviceMemory m;
    assert(l_AllocateMemory(D, &ci, &callbacks, &m) == VK_SUCCESS);
    assert(last_export == OPAQUE);
    return m;
}
static void test_mapping(void) {
    setup(1);
    create_device(1);
    VkDeviceMemory m = allocate();
    void *reservation = mmap(NULL, 4096, PROT_NONE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    assert(reservation != MAP_FAILED);
    VkMemoryMapPlacedInfoEXT p = {.sType = VK_STRUCTURE_TYPE_MEMORY_MAP_PLACED_INFO_EXT,
                                  .pPlacedAddress = reservation};
    VkMemoryMapInfoKHR info = {.sType = VK_STRUCTURE_TYPE_MEMORY_MAP_INFO_KHR,
                               .pNext = &p,
                               .flags = VK_MEMORY_MAP_PLACED_BIT_EXT,
                               .memory = m,
                               .size = VK_WHOLE_SIZE};
    void *out = NULL;
    info.offset = 4;
    assert(l_MapMemory2KHR(D, &info, &out) == VK_ERROR_MEMORY_MAP_FAILED);
    info.offset = 0;
    short_fd = 1;
    int closes = layer_closes;
    assert(l_MapMemory2KHR(D, &info, &out) == VK_ERROR_MEMORY_MAP_FAILED &&
           layer_closes == closes + 1);
    short_fd = 0;
    fail_native_map = 1;
    assert(l_MapMemory2KHR(D, &info, &out) == VK_ERROR_MEMORY_MAP_FAILED);
    fail_native_map = 0;
    fail_fixed_map = 1;
    unsigned before = native_unmaps;
    assert(l_MapMemory2KHR(D, &info, &out) == VK_ERROR_MEMORY_MAP_FAILED &&
           native_unmaps == before + 1);
    assert(!get_memory(get_device(D), m)->native);
    fail_fixed_map = 0;
    assert(l_MapMemory2KHR(D, &info, &out) == VK_SUCCESS && out == reservation);
    memset(out, 0x45, 4096);
    assert(((unsigned char *)((struct native_memory *)m)->map)[300] == 0x45);
    assert(l_MapMemory2KHR(D, &info, &out) == VK_ERROR_MEMORY_MAP_FAILED);
    VkMemoryUnmapInfoKHR ui = {.sType = VK_STRUCTURE_TYPE_MEMORY_UNMAP_INFO_KHR,
                               .memory = m,
                               .flags = VK_MEMORY_UNMAP_RESERVE_BIT_EXT};
    fail_reserve_map = 1;
    assert(l_UnmapMemory2KHR(D, &ui) == VK_ERROR_MEMORY_MAP_FAILED &&
           ((unsigned char *)out)[300] == 0x45);
    fail_reserve_map = 0;
    assert(l_UnmapMemory2KHR(D, &ui) == VK_SUCCESS);
    assert(!get_memory(get_device(D), m)->alias && !get_memory(get_device(D), m)->native);
    assert(mmap(reservation, 4096, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED,
                -1, 0) == reservation);
    memset(reservation, 0x67, 4096);
    closes = layer_closes;
    l_FreeMemory(D, m, &callbacks);
    assert(layer_closes == closes + 1);
    assert(((unsigned char *)reservation)[0] == 0x67);
    munmap(reservation, 4096);
    m = allocate();
    assert(l_MapMemory(D, m, 0, VK_WHOLE_SIZE, 0, &out) == VK_SUCCESS);
    l_UnmapMemory(D, m);
    l_FreeMemory(D, m, &callbacks);
    teardown();
    assert(native_maps == native_unmaps);
    puts("PASS mapping: alias coherence, duplicate/bounds rejection, map failure rollback, reserve "
         "failure preserves mapping, reserve/free avoids reused VA, fd/custom allocator cleanup");
}
static void *allocation_worker(void *unused) {
    (void)unused;
    for (int n = 0; n < 32; n++) {
        VkMemoryAllocateInfo ci = {.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
                                   .allocationSize = 4096,
                                   .memoryTypeIndex = 1};
        VkDeviceMemory m;
        assert(l_AllocateMemory(D, &ci, &callbacks, &m) == VK_SUCCESS);
        l_FreeMemory(D, m, &callbacks);
    }
    return NULL;
}
static void test_concurrency(void) {
    setup(1);
    create_device(1);
    pthread_t threads[4];
    for (unsigned n = 0; n < 4; n++)
        assert(!pthread_create(&threads[n], NULL, allocation_worker, NULL));
    for (unsigned n = 0; n < 4; n++)
        assert(!pthread_join(threads[n], NULL));
    assert(!get_device(D)->memories);
    VkBaseInStructure imported = {
        .sType = VK_STRUCTURE_TYPE_IMPORT_ANDROID_HARDWARE_BUFFER_INFO_ANDROID};
    VkMemoryAllocateInfo ci = {.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
                               .pNext = &imported,
                               .allocationSize = 4096,
                               .memoryTypeIndex = 1};
    VkDeviceMemory m;
    assert(l_AllocateMemory(D, &ci, &callbacks, &m) == VK_ERROR_INVALID_EXTERNAL_HANDLE);
    teardown();
    puts("PASS concurrency: 128 concurrent allocations/frees, reused device keys, unknown/import "
         "allocation chains fail closed");
}
int main(void) {
    test_dispatch();
    test_resources();
    test_mapping();
    test_concurrency();
    assert(explicit_failures == 1);
    printf(
        "PASS total: callback allocations=%u frees=%u native_maps=%u unmaps=%u driver_frees=%u\n",
        allocs, frees, native_maps, native_unmaps, driver_frees);
    return 0;
}
