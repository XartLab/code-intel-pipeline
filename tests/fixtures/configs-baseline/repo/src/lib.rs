//! Library half of the configs-baseline fixture repository.

/// Adds two coordinates. Kept trivial on purpose: the acceptance run needs
/// symbols to extract, not behaviour to test.
pub fn add(left: i64, right: i64) -> i64 {
    left + right
}

/// Multiplies two coordinates.
pub fn mul(left: i64, right: i64) -> i64 {
    left * right
}

#[cfg(test)]
mod tests {
    use super::{add, mul};

    #[test]
    fn add_and_mul_are_total() {
        assert_eq!(add(2, 3), 5);
        assert_eq!(mul(2, 3), 6);
    }
}