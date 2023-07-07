// Copyright (c) Meta Platforms, Inc. and affiliates.
// All rights reserved.
//
// This source code is licensed under the BSD-style license found in the
// LICENSE file in the root directory of this source tree.

#pragma once

#include <cstddef>
#include <filesystem>
#include <optional>
#include <string>

#include "fairseq2/native/api.h"
#include "fairseq2/native/memory.h"
#include "fairseq2/native/data/data.h"
#include "fairseq2/native/data/detail/lru_cache.h"

namespace fairseq2 {

class immutable_string;

class FAIRSEQ2_API memory_mapper {
    static constexpr std::size_t default_cached_fd_count = 100;

public:
    explicit
    memory_mapper(
        std::optional<std::string> root_dir,
        std::optional<std::size_t> cached_fd_count = {}) noexcept;

    data
    operator()(data &&d) const;

private:
    memory_block
    get_memory_map(const immutable_string &pathname) const;

private:
    std::filesystem::path root_dir_{};
    mutable detail::lru_cache<memory_block> cache_;
};

}  // namespace fairseq2
