//! Binary half of the configs-baseline fixture repository.

use configs_baseline_fixture::{add, mul};

fn main() {
    let total = add(2, 3) * mul(2, 3);
    println!("{total}");
}