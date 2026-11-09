// Isolated prefix-matching experiment; not part of Invariant's runtime.
// UTF-8 strings are packed contiguously, with count+1 byte offsets.
#include <cstdint>
#include <limits>
#include <string>
#include <unordered_map>

#if defined(PREFIX_PROBE_STANDALONE)
#include <chrono>
#include <iostream>
#include <vector>
#endif

#if defined(_WIN32)
#define EXPORT extern "C" __declspec(dllexport)
#else
#define EXPORT extern "C" __attribute__((visibility("default")))
#endif

struct Matcher {
    std::unordered_map<std::string, std::int64_t> prefixes;
};

EXPORT void* prefix_matcher_create(const char* bytes, const std::uint64_t* offsets,
                                  std::uint64_t count) noexcept {
    try {
        Matcher* matcher = new Matcher;
        try {
            matcher->prefixes.reserve(static_cast<std::size_t>(count));
            for (std::uint64_t i = 0; i < count; ++i) {
                // emplace preserves the first configured duplicate.
                matcher->prefixes.emplace(
                    std::string(bytes + offsets[i], offsets[i + 1] - offsets[i]),
                    static_cast<std::int64_t>(i));
            }
        } catch (...) {
            delete matcher;
            throw;
        }
        return matcher;
    } catch (...) {
        return nullptr;
    }
}

EXPORT void prefix_matcher_destroy(void* opaque) noexcept {
    delete static_cast<Matcher*>(opaque);
}

EXPORT int prefix_matcher_batch(const void* opaque, const char* bytes,
                               const std::uint64_t* offsets, std::uint64_t count,
                               std::int64_t* result) noexcept {
    try {
        const Matcher& matcher = *static_cast<const Matcher*>(opaque);
        for (std::uint64_t i = 0; i < count; ++i) {
            std::string name(bytes + offsets[i], offsets[i + 1] - offsets[i]);
            std::int64_t first = std::numeric_limits<std::int64_t>::max();
            for (;;) {
                const auto found = matcher.prefixes.find(name);
                if (found != matcher.prefixes.end() && found->second < first)
                    first = found->second;
                const std::size_t dot = name.rfind('.');
                if (dot == std::string::npos)
                    break;
                name.resize(dot);
            }
            result[i] = first == std::numeric_limits<std::int64_t>::max() ? -1 : first;
        }
        return 0;
    } catch (...) {
        return -1;
    }
}

#if defined(PREFIX_PROBE_STANDALONE)
// Fallback when the installed compiler and Python have different bitness.
// Text input: prefix/name/repeat/warmup counts, then one UTF-8 name/line.
int main() {
    std::ios::sync_with_stdio(false);
    std::uint64_t prefixes, names, repeat, warmup;
    if (!(std::cin >> prefixes >> names >> repeat >> warmup))
        return 1;
    std::string line;
    std::getline(std::cin, line);
    auto read = [&](std::uint64_t count, std::string& data,
                    std::vector<std::uint64_t>& offsets) {
        offsets.push_back(0);
        for (std::uint64_t i = 0; i < count; ++i) {
            if (!std::getline(std::cin, line))
                return false;
            data += line;
            offsets.push_back(data.size());
        }
        return true;
    };
    std::string prefix_data, name_data;
    std::vector<std::uint64_t> prefix_offsets, name_offsets;
    if (!read(prefixes, prefix_data, prefix_offsets) ||
        !read(names, name_data, name_offsets))
        return 2;
    void* matcher = prefix_matcher_create(prefix_data.data(), prefix_offsets.data(), prefixes);
    if (!matcher)
        return 3;
    std::vector<std::int64_t> result(names);
    std::vector<double> samples;
    // Kernel samples request a warm-up; full-process samples execute once.
    for (std::uint64_t i = 0; i < warmup; ++i)
        prefix_matcher_batch(matcher, name_data.data(), name_offsets.data(), names, result.data());
    for (std::uint64_t i = 0; i < repeat; ++i) {
        const auto began = std::chrono::steady_clock::now();
        const int status = prefix_matcher_batch(
            matcher, name_data.data(), name_offsets.data(), names, result.data());
        const auto elapsed = std::chrono::steady_clock::now() - began;
        if (status != 0) {
            prefix_matcher_destroy(matcher);
            return 4;
        }
        samples.push_back(std::chrono::duration<double, std::milli>(elapsed).count());
    }
    prefix_matcher_destroy(matcher);
    std::cout << "{\"kernel_ms\":[";
    for (std::size_t i = 0; i < samples.size(); ++i) {
        if (i)
            std::cout << ',';
        std::cout << samples[i];
    }
    std::cout << "],\"matches\":[";
    for (std::size_t i = 0; i < result.size(); ++i) {
        if (i)
            std::cout << ',';
        std::cout << result[i];
    }
    std::cout << "]}";
}
#endif
