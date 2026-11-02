import { greet } from "../src/index";

test("greets", () => {
  expect(greet("world")).toBe("hello world");
});
