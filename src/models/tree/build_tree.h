/* Copyright 2023 NVIDIA Corporation
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 */
#pragma once
#include "legate_library.h"
#include "legateboost.h"

namespace legateboost {

inline const double eps = 1e-5;  // Add this term to the hessian to prevent division by zero

// Some helpers for indexing into a binary tree
class BinaryTree {
 public:
  __host__ __device__ static int Parent(int i) { return (i - 1) / 2; }
  __host__ __device__ static int LeftChild(int i) { return 2 * i + 1; }
  __host__ __device__ static int RightChild(int i) { return 2 * i + 2; }
  __host__ __device__ static int LevelBegin(int level) { return (1 << level) - 1; }
  __host__ __device__ static int NodesInLevel(int level) { return 1 << level; }
};

// Estimate if the left or right child has less data
// We compute the histogram for the child with less data
// And infer the other side by subtraction from the parent
inline __host__ __device__ std::pair<int, int> SelectHistogramNode(
  int parent, legate::Buffer<double, 2> node_hessians)
{
  int left_child  = BinaryTree::LeftChild(parent);
  int right_child = BinaryTree::RightChild(parent);
  if (node_hessians[{left_child, 0}] < node_hessians[{right_child, 0}]) {
    return {left_child, right_child};
  }
  return {right_child, left_child};
}

inline __host__ __device__ bool ComputeHistogramBin(int node_id,
                                                    int depth,
                                                    legate::Buffer<double, 2> node_hessians)
{
  if (node_id == 0) return true;
  if (node_id < 0) return false;
  int parent                           = BinaryTree::Parent(node_id);
  auto [histogram_node, subtract_node] = SelectHistogramNode(parent, node_hessians);
  return histogram_node == node_id;
}

__host__ __device__ inline double CalculateLeafValue(double G, double H, double alpha)
{
  return -G / (H + alpha);
}

struct GPair {
  double grad = 0.0;
  double hess = 0.0;

  __host__ __device__ GPair& operator+=(const GPair& b)
  {
    this->grad += b.grad;
    this->hess += b.hess;
    return *this;
  }
};

inline __host__ __device__ GPair operator-(const GPair& a, const GPair& b)
{
  return GPair{a.grad - b.grad, a.hess - b.hess};
}

inline __host__ __device__ GPair operator+(const GPair& a, const GPair& b)
{
  return GPair{a.grad + b.grad, a.hess + b.hess};
}

class BuildTreeTask : public Task<BuildTreeTask, BUILD_TREE> {
 public:
  static void cpu_variant(legate::TaskContext context);
#ifdef LEGATEBOOST_USE_CUDA
  static void gpu_variant(legate::TaskContext context);
#endif
};

}  // namespace legateboost
